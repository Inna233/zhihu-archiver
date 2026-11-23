from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
from pathlib import Path
from typing import Any, Literal

from zhihu_archiver.exporter import export_archive
from zhihu_archiver.models import ArchiveItem, ItemType, PageResult
from zhihu_archiver.providers.base import (
    AuthError,
    DuplicateCursorError,
    FatalProviderError,
    ProviderError,
    QuotaError,
    ZhihuProvider,
)
from zhihu_archiver.storage import ArchiveStore, ProgressState

logger = logging.getLogger(__name__)

RunMode = Literal["preview", "sample", "full"]


class BudgetExhaustedError(Exception):
    """Raised when the per-run request budget is exhausted."""


@dataclass(slots=True)
class RequestBudget:
    max_requests: int
    used: int = 0

    def consume(self) -> None:
        if self.max_requests > 0 and self.used >= self.max_requests:
            raise BudgetExhaustedError(
                f"Reached per-run request limit ({self.max_requests}). Re-run to continue."
            )
        self.used += 1


@dataclass(slots=True)
class ArchiveConfig:
    user_url_token: str
    output_dir: Path
    mode: RunMode = "sample"
    item_types: tuple[ItemType, ...] = ("article", "pin")
    answer_sort: Literal["created", "voteups"] = "created"
    page_limit: int = 20
    sample_pages: int = 2
    max_requests: int = 10
    fetch_user_info: bool = False
    download_images: bool = False


@dataclass(slots=True)
class RunSummary:
    mode: RunMode
    requests_used: int
    manifest: dict[str, Any]
    sample_report: dict[str, Any] | None = None
    halted_reason: str = ""
    mismatched_author_count: int = 0


class ArchivePipeline:
    """Coordinates listing, deduplication, detail backfill, persistence, and export."""

    def __init__(
        self,
        provider: ZhihuProvider,
        store: ArchiveStore,
        config: ArchiveConfig,
    ) -> None:
        self.provider = provider
        self.store = store
        self.config = config
        self.budget = RequestBudget(max_requests=config.max_requests)
        self._target_token = config.user_url_token.lower()
        self._target_author_id = store.get_meta("author_id")
        self._mismatched_author_count = 0

    def _is_target_author(self, item: ArchiveItem) -> bool:
        token = item.author_url_token.lower().strip()
        aid = item.author_id.strip()
        if token:
            if token != self._target_token:
                return False
            if aid and not self._target_author_id:
                self._target_author_id = aid
                self.store.set_meta("author_id", aid)
            if item.author_name and not self.store.get_meta("author_name"):
                self.store.set_meta("author_name", item.author_name)
            return True
        if aid and self._target_author_id:
            return aid == self._target_author_id
        # If upstream omits author fields entirely on a detail endpoint, accept cautiously
        return True

    def _fetch_list_page(
        self,
        item_type: ItemType,
        offset: int,
        cursor: str | None,
    ) -> PageResult:
        self.budget.consume()
        if item_type == "article":
            return self.provider.fetch_user_articles(
                self.config.user_url_token,
                offset=offset,
                limit=self.config.page_limit,
                cursor=cursor,
            )
        if item_type == "pin":
            return self.provider.fetch_user_pins(
                self.config.user_url_token,
                offset=offset,
                limit=self.config.page_limit,
                cursor=cursor,
            )
        if item_type == "answer":
            return self.provider.fetch_user_answers(
                self.config.user_url_token,
                offset=offset,
                limit=self.config.page_limit,
                sort_by=self.config.answer_sort,
                cursor=cursor,
            )
        return self.provider.fetch_user_questions(
            self.config.user_url_token,
            offset=offset,
            limit=self.config.page_limit,
            cursor=cursor,
        )

    def _fetch_detail(self, item_type: ItemType, item_id: str) -> ArchiveItem:
        self.budget.consume()
        if item_type == "article":
            return self.provider.fetch_article_detail(item_id)
        if item_type == "pin":
            return self.provider.fetch_pin_detail(item_id)
        if item_type == "answer":
            return self.provider.fetch_answer_detail(item_id)
        raise ProviderError(f"Detail fetch is not required/supported for item_type={item_type!r}")

    def _paginate_type(
        self,
        item_type: ItemType,
        *,
        max_pages: int | None = None,
    ) -> dict[str, Any]:
        prog = self.store.get_progress(item_type)
        stats: dict[str, Any] = {
            "pages_fetched_this_run": 0,
            "page_new_ids": [],
            "stopped_reason": "",
            "fatal_global": False,
        }

        if prog.is_end and prog.status == "completed":
            logger.info("[%s] List already completed at offset=%d.", item_type, prog.next_offset)
            return stats

        existing_ids = self.store.get_existing_ids(item_type)
        pages_this_run = 0
        consecutive_zero_new = 0

        while True:
            if max_pages is not None and pages_this_run >= max_pages:
                break

            req_offset = prog.next_offset
            req_cursor = prog.next_cursor
            try:
                page = self._fetch_list_page(item_type, offset=req_offset, cursor=req_cursor)
            except BudgetExhaustedError as exc:
                self.store.update_progress_status(
                    item_type, status="budget_reached", stop_reason=str(exc)
                )
                stats["stopped_reason"] = str(exc)
                break
            except (AuthError, QuotaError) as exc:
                self.store.record_failure(
                    kind="list",
                    item_type=item_type,
                    offset=req_offset,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                self.store.update_progress_status(
                    item_type, status="stopped_error", stop_reason=str(exc)
                )
                stats["stopped_reason"] = str(exc)
                stats["fatal_global"] = True
                break
            except ProviderError as exc:
                self.store.record_failure(
                    kind="list",
                    item_type=item_type,
                    offset=req_offset,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                self.store.update_progress_status(
                    item_type, status="stopped_error", stop_reason=str(exc)
                )
                stats["stopped_reason"] = str(exc)
                break

            verified_items: list[ArchiveItem] = []
            for item in page.items:
                if self._is_target_author(item):
                    verified_items.append(item)
                else:
                    self._mismatched_author_count += 1
                    logger.warning(
                        "[%s] Skipping item id=%s with mismatched author (%s != %s)",
                        item_type,
                        item.id,
                        item.author_url_token,
                        self.config.user_url_token,
                    )

            page_ids = [item.id for item in verified_items]
            new_ids = [iid for iid in page_ids if iid not in existing_ids]
            stats["page_new_ids"].append(len(new_ids))

            if page_ids and len(new_ids) == 0:
                consecutive_zero_new += 1
            else:
                consecutive_zero_new = 0

            # Duplicate page / stuck cursor detection:
            # Allow a single overlapping window (e.g. Zhihu returning 40 items at offset=20 then 20 overlapping
            # items at offset=40) as long as page_ids differ and offset advances, but halt immediately on exact
            # repeated page or 2 consecutive zero-new-ID pages.
            is_duplicate_page = bool(page_ids) and (
                page_ids == prog.last_page_ids
                or (req_offset > 0 and consecutive_zero_new >= 2)
            )
            if is_duplicate_page:
                err = DuplicateCursorError(
                    f"Duplicate page detected for {item_type} at offset={req_offset} "
                    f"(0 new IDs out of {len(page_ids)} items)."
                )
                self.store.record_failure(
                    kind="list",
                    item_type=item_type,
                    offset=req_offset,
                    error_type=type(err).__name__,
                    error_message=str(err),
                )
                self.store.update_progress_status(
                    item_type, status="stopped_error", stop_reason=str(err)
                )
                stats["stopped_reason"] = str(err)
                break

            reached_end = page.is_end or len(page.items) == 0
            next_offset = (
                page.next_offset
                if page.next_offset is not None
                else req_offset + len(page.items)
            )
            if not reached_end and next_offset <= req_offset:
                err = DuplicateCursorError(
                    f"Pagination offset did not advance for {item_type}: "
                    f"requested={req_offset}, next={next_offset}."
                )
                self.store.record_failure(
                    kind="list",
                    item_type=item_type,
                    offset=req_offset,
                    error_type=type(err).__name__,
                    error_message=str(err),
                )
                self.store.update_progress_status(
                    item_type, status="stopped_error", stop_reason=str(err)
                )
                stats["stopped_reason"] = str(err)
                break

            next_prog = ProgressState(
                item_type=item_type,
                next_offset=next_offset,
                next_cursor=page.next_cursor,
                pages_fetched=prog.pages_fetched + 1,
                enumerated_count=prog.enumerated_count + len(verified_items),
                api_total=page.api_total if page.api_total is not None else prog.api_total,
                is_end=reached_end,
                status="completed" if reached_end else "in_progress",
                last_page_ids=page_ids,
                stop_reason="",
            )
            self.store.save_page_and_progress(item_type, verified_items, next_prog)
            existing_ids.update(new_ids)
            prog = next_prog
            pages_this_run += 1
            stats["pages_fetched_this_run"] = pages_this_run

            if reached_end:
                break

        return stats

    def _backfill_details(self, item_type: ItemType) -> dict[str, Any]:
        pending = self.store.get_items_needing_detail(item_type)
        result = {"attempted": 0, "succeeded": 0, "failed": 0, "fatal_global": False, "stopped_reason": ""}

        for item in pending:
            try:
                detail = self._fetch_detail(item_type, item.id)
            except BudgetExhaustedError as exc:
                result["stopped_reason"] = str(exc)
                break
            except (AuthError, QuotaError) as exc:
                self.store.record_failure(
                    kind="detail",
                    item_type=item_type,
                    item_id=item.id,
                    url=item.url,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                result["failed"] += 1
                result["fatal_global"] = True
                result["stopped_reason"] = str(exc)
                break
            except ProviderError as exc:
                self.store.record_failure(
                    kind="detail",
                    item_type=item_type,
                    item_id=item.id,
                    url=item.url,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                result["failed"] += 1
                continue

            result["attempted"] += 1
            if not detail.id:
                detail.id = item.id
            if not detail.author_url_token:
                detail.author_url_token = item.author_url_token
            if not detail.author_id:
                detail.author_id = item.author_id
            if not detail.author_name:
                detail.author_name = item.author_name
            if not detail.created_at:
                detail.created_at = item.created_at
            if not detail.updated_at:
                detail.updated_at = item.updated_at
            self.store.save_detail_item(detail)
            result["succeeded"] += 1

        return result

    def _run_sample_detail_check(self, item_type: ItemType) -> dict[str, Any]:
        items = self.store.get_all_items(item_type)
        if not items:
            return {"checked": False, "reason": "no_items"}
        sample = items[0]
        list_len = len(sample.content_markdown)
        list_images = len(sample.images)
        list_has_quote = sample.quote is not None

        try:
            detail = self._fetch_detail(item_type, sample.id)
        except (BudgetExhaustedError, ProviderError) as exc:
            self.store.record_failure(
                kind="detail",
                item_type=item_type,
                item_id=sample.id,
                url=sample.url,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return {
                "checked": False,
                "item_id": sample.id,
                "url": sample.url,
                "error": str(exc),
            }

        if not detail.id:
            detail.id = sample.id
        if not detail.author_url_token:
            detail.author_url_token = sample.author_url_token
        if not detail.author_id:
            detail.author_id = sample.author_id
        if not detail.author_name:
            detail.author_name = sample.author_name
        self.store.save_detail_item(detail)

        detail_len = len(detail.content_markdown)
        list_truncated = detail_len > list_len + 20 or not sample.is_full_content
        return {
            "checked": True,
            "item_id": sample.id,
            "url": detail.url,
            "url_matches_list": detail.url == sample.url,
            "list_content_chars": list_len,
            "detail_content_chars": detail_len,
            "list_appeared_truncated": list_truncated,
            "list_images_count": list_images,
            "detail_images_count": len(detail.images),
            "content_blocks_count": detail.content_blocks_count,
            "has_quote": detail.quote is not None or list_has_quote,
        }

    def run(self) -> RunSummary:
        self.store.set_meta("user_url_token", self.config.user_url_token)
        self.store.set_meta("provider", self.provider.name)

        halted_reason = ""

        if self.config.fetch_user_info or self.config.mode == "preview":
            try:
                self.budget.consume()
                profile = self.provider.fetch_user_info(self.config.user_url_token)
                if profile:
                    if profile.member_id:
                        self._target_author_id = profile.member_id
                        self.store.set_meta("author_id", profile.member_id)
                    if profile.name:
                        self.store.set_meta("author_name", profile.name)
                    for meta_key, val in (
                        ("profile_articles_count", profile.articles_count),
                        ("profile_pins_count", profile.pins_count),
                        ("profile_answers_count", profile.answers_count),
                        ("profile_questions_count", profile.questions_count),
                        ("profile_follower_count", profile.follower_count),
                    ):
                        if val is not None:
                            self.store.set_meta(meta_key, str(val))
            except (BudgetExhaustedError, ProviderError) as exc:
                logger.warning("fetch_user_info skipped/failed: %s", exc)
                if self.config.mode == "preview":
                    halted_reason = str(exc)

        sample_report: dict[str, Any] | None = None

        if self.config.mode == "preview":
            pass
        elif self.config.mode == "sample":
            list_reports: dict[str, Any] = {}
            detail_checks: dict[str, Any] = {}
            for t in self.config.item_types:
                list_stats = self._paginate_type(t, max_pages=self.config.sample_pages)
                list_reports[t] = list_stats
                if list_stats["fatal_global"]:
                    halted_reason = list_stats["stopped_reason"]
                    break
                if t in {"article", "pin", "answer"}:
                    detail_checks[t] = self._run_sample_detail_check(t)

            # Estimate full-run requests
            est_list_reqs = 0
            est_detail_reqs = 0
            for t in self.config.item_types:
                prog = self.store.get_progress(t)
                items = self.store.get_all_items(t)
                total = prog.api_total or len(items)
                pages_needed = math.ceil(total / max(1, self.config.page_limit))
                est_list_reqs += pages_needed
                truncated_ratio = (
                    sum(1 for it in items if it.needs_detail) / len(items)
                    if items
                    else 0.0
                )
                if detail_checks.get(t, {}).get("list_appeared_truncated"):
                    truncated_ratio = max(truncated_ratio, 1.0)
                est_detail_reqs += math.ceil(total * truncated_ratio)

            sample_report = {
                "lists": list_reports,
                "detail_cross_checks": detail_checks,
                "estimated_full_run": {
                    "list_requests": est_list_reqs,
                    "detail_requests": est_detail_reqs,
                    "total_requests": est_list_reqs + est_detail_reqs,
                    "estimated_tikhub_usd_at_0_001": round((est_list_reqs + est_detail_reqs) * 0.001, 4),
                },
            }
        else:
            for t in self.config.item_types:
                list_stats = self._paginate_type(t, max_pages=None)
                if list_stats["fatal_global"]:
                    halted_reason = list_stats["stopped_reason"]
                    break
                if list_stats["stopped_reason"]:
                    halted_reason = list_stats["stopped_reason"]
                    if "Reached per-run request limit" in halted_reason:
                        break
                bf = self._backfill_details(t)
                if bf["fatal_global"]:
                    halted_reason = bf["stopped_reason"]
                    break
                if bf["stopped_reason"] and not halted_reason:
                    halted_reason = bf["stopped_reason"]
                    if "Reached per-run request limit" in halted_reason:
                        break

        manifest = export_archive(
            self.store,
            self.config.output_dir,
            run_mode=self.config.mode,
            item_types=self.config.item_types,
            sample_report=sample_report,
            download_images=self.config.download_images,
        )
        return RunSummary(
            mode=self.config.mode,
            requests_used=self.budget.used,
            manifest=manifest,
            sample_report=sample_report,
            halted_reason=halted_reason,
            mismatched_author_count=self._mismatched_author_count,
        )
