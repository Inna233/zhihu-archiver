from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from zhihu_archiver.converter import html_to_markdown, pin_blocks_to_markdown
from zhihu_archiver.models import (
    ArchiveItem,
    PageResult,
    QuoteContent,
    UserProfile,
    timestamp_to_iso,
)
from zhihu_archiver.providers.base import (
    AuthError,
    ProviderError,
    QuotaError,
    RateLimitError,
    TransientError,
    ZhihuProvider,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.tikhub.io/api/v1/zhihu/web"


def _canonical_article_url(article_id: str, raw_url: str = "") -> str:
    if article_id:
        return f"https://zhuanlan.zhihu.com/p/{article_id}"
    return raw_url.replace("http://", "https://", 1) if raw_url.startswith("http://") else raw_url


def _canonical_pin_url(pin_id: str, raw_url: str = "") -> str:
    if pin_id:
        return f"https://www.zhihu.com/pin/{pin_id}"
    return raw_url.replace("http://", "https://", 1) if raw_url.startswith("http://") else raw_url


def _canonical_question_url(question_id: str, raw_url: str = "") -> str:
    if question_id:
        return f"https://www.zhihu.com/question/{question_id}"
    return raw_url.replace("http://", "https://", 1) if raw_url.startswith("http://") else raw_url


def _canonical_answer_url(question_id: str, answer_id: str, raw_url: str = "") -> str:
    if question_id and answer_id:
        return f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
    if answer_id:
        return f"https://www.zhihu.com/answer/{answer_id}"
    return raw_url.replace("http://", "https://", 1) if raw_url.startswith("http://") else raw_url


def _opt_int(val: Any) -> int | None:
    return val if isinstance(val, int) and not isinstance(val, bool) else None


def _extract_offset_from_url(next_url: str | None) -> int | None:
    if not next_url:
        return None
    parsed = urlparse(next_url)
    vals = parse_qs(parsed.query).get("offset")
    if vals and vals[0].isdigit():
        return int(vals[0])
    return None


def _looks_truncated(content_md: str, excerpt_md: str, has_html_content: bool) -> bool:
    if not has_html_content or not content_md.strip():
        return True
    stripped = content_md.strip()
    if excerpt_md and stripped == excerpt_md.strip() and stripped.endswith(("...", "…")):
        return True
    return False


def parse_article_dict(
    raw: dict[str, Any],
    *,
    from_detail: bool = False,
) -> ArchiveItem:
    """Normalize a Zhihu article JSON object from list or detail endpoints."""
    article_id = str(raw.get("id") or raw.get("article_id") or "")
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    author_id = str(author.get("id") or "")
    author_url_token = str(author.get("url_token") or "")
    author_name = str(author.get("name") or "")

    title = str(raw.get("title") or "").strip()
    raw_url = str(raw.get("url") or "")
    url = _canonical_article_url(article_id, raw_url)

    created_at = timestamp_to_iso(raw.get("created") or raw.get("created_time"))
    updated_at = timestamp_to_iso(raw.get("updated") or raw.get("updated_time"))

    raw_content = str(raw.get("content") or "")
    raw_excerpt = str(raw.get("excerpt") or raw.get("excerpt_title") or "")
    excerpt_md, _ = html_to_markdown(raw_excerpt)

    images: list[str] = []
    seen_images: set[str] = set()

    def add_img(u: str) -> None:
        if u and u not in seen_images:
            seen_images.add(u)
            images.append(u)

    title_image = str(raw.get("title_image") or raw.get("image_url") or "").strip()
    if title_image:
        add_img(title_image)

    content_md, body_imgs = html_to_markdown(raw_content)
    for img in body_imgs:
        add_img(img)

    has_content = bool(raw_content.strip())
    truncated = False if from_detail else _looks_truncated(content_md, excerpt_md, has_content)
    is_full = bool(content_md.strip()) and not truncated

    return ArchiveItem(
        type="article",
        id=article_id,
        url=url,
        author_url_token=author_url_token,
        author_id=author_id,
        author_name=author_name,
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        content_markdown=content_md if content_md else excerpt_md,
        excerpt=excerpt_md,
        images=images,
        voteup_count=_opt_int(raw.get("voteup_count")),
        comment_count=_opt_int(raw.get("comment_count")),
        content_blocks_count=1 if content_md else 0,
        is_full_content=is_full,
        needs_detail=not is_full,
        detail_fetched=from_detail,
        raw_json=raw,
    )


def _parse_origin_pin(origin: dict[str, Any]) -> QuoteContent:
    origin_id = str(origin.get("id") or "")
    author = origin.get("author") if isinstance(origin.get("author"), dict) else {}
    raw_blocks = origin.get("content") if isinstance(origin.get("content"), list) else None
    raw_html = str(
        origin.get("content_html")
        or (origin.get("content") if isinstance(origin.get("content"), str) else "")
        or origin.get("excerpt_title")
        or ""
    )
    md, imgs, _ = pin_blocks_to_markdown(raw_blocks, fallback_html=raw_html)
    origin_type = str(origin.get("type") or "pin")
    raw_url = str(origin.get("url") or "")
    if origin_type == "article" and origin_id:
        url = _canonical_article_url(origin_id, raw_url)
    elif origin_id:
        url = _canonical_pin_url(origin_id, raw_url)
    else:
        url = raw_url

    return QuoteContent(
        quote_type=origin_type,
        id=origin_id,
        title=str(origin.get("title") or "").strip(),
        url=url,
        author_id=str(author.get("id") or ""),
        author_url_token=str(author.get("url_token") or ""),
        author_name=str(author.get("name") or ""),
        created_at=timestamp_to_iso(origin.get("created") or origin.get("created_time")),
        content_markdown=md,
        images=imgs,
    )


def parse_pin_dict(
    raw: dict[str, Any],
    *,
    from_detail: bool = False,
) -> ArchiveItem:
    """Normalize a Zhihu pin (想法) JSON object from list or detail endpoints."""
    pin_id = str(raw.get("id") or raw.get("pin_id") or "")
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    author_id = str(author.get("id") or "")
    author_url_token = str(author.get("url_token") or "")
    author_name = str(author.get("name") or "")

    raw_url = str(raw.get("url") or "")
    url = _canonical_pin_url(pin_id, raw_url)

    created_at = timestamp_to_iso(raw.get("created") or raw.get("created_time"))
    updated_at = timestamp_to_iso(raw.get("updated") or raw.get("updated_time"))

    raw_blocks = raw.get("content") if isinstance(raw.get("content"), list) else None
    raw_html = str(
        raw.get("content_html")
        or (raw.get("content") if isinstance(raw.get("content"), str) else "")
        or ""
    )
    excerpt_raw = str(raw.get("excerpt_title") or raw.get("excerpt") or "")
    excerpt_md, _ = html_to_markdown(excerpt_raw)

    content_md, images, blocks_count = pin_blocks_to_markdown(
        raw_blocks, fallback_html=raw_html
    )

    quote: QuoteContent | None = None
    origin_pin = raw.get("origin_pin")
    if isinstance(origin_pin, dict) and origin_pin:
        quote = _parse_origin_pin(origin_pin)

    # Determine if pin content is complete:
    # - Has content blocks or HTML content (not just an ellipsis-truncated excerpt)
    # - Or is a pure repost where origin_pin carries the content and author left no commentary
    has_own_content = bool(content_md.strip())
    has_quote_content = bool(quote and quote.content_markdown.strip())

    if from_detail:
        is_full = has_own_content or has_quote_content
    else:
        truncated_excerpt = (
            blocks_count == 0
            and not raw_html.strip()
            and bool(excerpt_md)
        )
        is_full = (has_own_content or has_quote_content) and not truncated_excerpt

    return ArchiveItem(
        type="pin",
        id=pin_id,
        url=url,
        author_url_token=author_url_token,
        author_id=author_id,
        author_name=author_name,
        title="",
        created_at=created_at,
        updated_at=updated_at,
        content_markdown=content_md if content_md else excerpt_md,
        excerpt=excerpt_md,
        images=images,
        quote=quote,
        voteup_count=_opt_int(raw.get("like_count") if "like_count" in raw else raw.get("reaction_count")),
        comment_count=_opt_int(raw.get("comment_count")),
        content_blocks_count=blocks_count,
        is_full_content=is_full,
        needs_detail=not is_full,
        detail_fetched=from_detail,
        raw_json=raw,
    )


def parse_answer_dict(
    raw: dict[str, Any],
    *,
    from_detail: bool = False,
) -> ArchiveItem:
    """Normalize a Zhihu answer JSON object from list or detail endpoints."""
    answer_id = str(raw.get("id") or raw.get("answer_id") or "")
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    author_id = str(author.get("id") or "")
    author_url_token = str(author.get("url_token") or "")
    author_name = str(author.get("name") or "")

    question = raw.get("question") if isinstance(raw.get("question"), dict) else {}
    question_id = str(question.get("id") or raw.get("question_id") or "")
    question_title = str(question.get("title") or raw.get("title") or "").strip()
    question_url = _canonical_question_url(question_id, str(question.get("url") or ""))

    raw_url = str(raw.get("url") or "")
    url = _canonical_answer_url(question_id, answer_id, raw_url)

    created_at = timestamp_to_iso(raw.get("created_time") or raw.get("created"))
    updated_at = timestamp_to_iso(raw.get("updated_time") or raw.get("updated"))

    raw_content = str(raw.get("content") or "")
    raw_excerpt = str(raw.get("excerpt") or "")
    excerpt_md, _ = html_to_markdown(raw_excerpt)
    content_md, images = html_to_markdown(raw_content)

    has_content = bool(raw_content.strip())
    truncated = False if from_detail else _looks_truncated(content_md, excerpt_md, has_content)
    is_full = bool(content_md.strip()) and not truncated

    return ArchiveItem(
        type="answer",
        id=answer_id,
        url=url,
        author_url_token=author_url_token,
        author_id=author_id,
        author_name=author_name,
        title=question_title,
        created_at=created_at,
        updated_at=updated_at,
        content_markdown=content_md if content_md else excerpt_md,
        excerpt=excerpt_md,
        images=images,
        question_id=question_id,
        question_title=question_title,
        question_url=question_url,
        voteup_count=_opt_int(raw.get("voteup_count")),
        comment_count=_opt_int(raw.get("comment_count")),
        content_blocks_count=1 if content_md else 0,
        is_full_content=is_full,
        needs_detail=not is_full,
        detail_fetched=from_detail,
        raw_json=raw,
    )


def parse_question_dict(
    raw: dict[str, Any],
    *,
    from_detail: bool = False,
) -> ArchiveItem:
    """Normalize a Zhihu question JSON object from list endpoints."""
    question_id = str(raw.get("id") or "")
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    author_id = str(author.get("id") or "")
    author_url_token = str(author.get("url_token") or "")
    author_name = str(author.get("name") or "")

    title = str(raw.get("title") or "").strip()
    url = _canonical_question_url(question_id, str(raw.get("url") or ""))
    created_at = timestamp_to_iso(raw.get("created") or raw.get("created_time"))
    updated_at = timestamp_to_iso(raw.get("updated_time") or raw.get("updated"))

    raw_detail = str(raw.get("detail") or raw.get("excerpt") or "")
    content_md, images = html_to_markdown(raw_detail)

    return ArchiveItem(
        type="question",
        id=question_id,
        url=url,
        author_url_token=author_url_token,
        author_id=author_id,
        author_name=author_name,
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        content_markdown=content_md,
        excerpt=content_md[:200] if content_md else "",
        images=images,
        question_id=question_id,
        question_title=title,
        question_url=url,
        voteup_count=_opt_int(raw.get("follower_count")),
        comment_count=_opt_int(raw.get("answer_count")),
        content_blocks_count=1 if content_md else 0,
        is_full_content=True,
        needs_detail=False,
        detail_fetched=from_detail,
        raw_json=raw,
    )


class TikHubProvider(ZhihuProvider):
    """TikHub Zhihu Web API provider."""

    name = "tikhub"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 1.5,
        client: httpx.Client | None = None,
    ) -> None:
        resolved_key = api_key if api_key is not None else os.environ.get("TIKHUB_API_KEY", "")
        resolved_key = resolved_key.strip()
        if not resolved_key:
            raise AuthError(
                "TIKHUB_API_KEY is not set. Export TIKHUB_API_KEY in your environment before calling TikHub."
            )
        self._api_key = resolved_key
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(0, max_retries)
        self._backoff_base = backoff_base
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "User-Agent": "zhihu-archiver/0.1",
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request_json(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_err = TransientError(f"Network error calling {endpoint}: {exc}")
                if attempt < self._max_retries:
                    sleep_s = self._backoff_base * (2**attempt)
                    logger.warning(
                        "Transient error on %s (attempt %d/%d), retrying in %.1fs",
                        endpoint,
                        attempt + 1,
                        self._max_retries + 1,
                        sleep_s,
                    )
                    time.sleep(sleep_s)
                    continue
                raise last_err from exc

            status = resp.status_code
            if status in (401, 403):
                raise AuthError(f"TikHub authentication/permission error (HTTP {status}) on {endpoint}.")
            if status == 402:
                raise QuotaError(f"TikHub insufficient balance/quota (HTTP 402) on {endpoint}.")
            if status == 429:
                retry_after_hdr = resp.headers.get("Retry-After")
                retry_after = float(retry_after_hdr) if retry_after_hdr and retry_after_hdr.isdigit() else None
                last_err = RateLimitError(f"TikHub rate limited (HTTP 429) on {endpoint}.", retry_after=retry_after)
                if attempt < self._max_retries:
                    sleep_s = retry_after if retry_after is not None else self._backoff_base * (2**attempt)
                    logger.warning(
                        "Rate limited on %s (attempt %d/%d), sleeping %.1fs",
                        endpoint,
                        attempt + 1,
                        self._max_retries + 1,
                        sleep_s,
                    )
                    time.sleep(sleep_s)
                    continue
                raise last_err
            if status >= 500:
                last_err = TransientError(f"TikHub server error (HTTP {status}) on {endpoint}.")
                if attempt < self._max_retries:
                    sleep_s = self._backoff_base * (2**attempt)
                    logger.warning(
                        "Server error %d on %s (attempt %d/%d), retrying in %.1fs",
                        status,
                        endpoint,
                        attempt + 1,
                        self._max_retries + 1,
                        sleep_s,
                    )
                    time.sleep(sleep_s)
                    continue
                raise last_err
            if status >= 400:
                raise ProviderError(f"TikHub request failed (HTTP {status}) on {endpoint}: {resp.text[:200]}")

            try:
                payload = resp.json()
            except ValueError as exc:
                raise ProviderError(f"Invalid JSON from TikHub {endpoint}") from exc

            if not isinstance(payload, dict):
                raise ProviderError(f"Unexpected non-object JSON from TikHub {endpoint}")

            code = payload.get("code")
            if code is not None and code != 200:
                msg = str(payload.get("message") or payload.get("detail") or "")
                low = msg.lower()
                if code in (401, 403) or "unauthorized" in low or "invalid api key" in low:
                    raise AuthError(f"TikHub auth error (code={code}): {msg}")
                if code == 402 or "balance" in low or "quota" in low or "credit" in low:
                    raise QuotaError(f"TikHub quota error (code={code}): {msg}")
                if code == 429 or "rate limit" in low:
                    last_err = RateLimitError(f"TikHub rate limit (code={code}): {msg}")
                    if attempt < self._max_retries:
                        time.sleep(self._backoff_base * (2**attempt))
                        continue
                    raise last_err
                if isinstance(code, int) and code >= 500:
                    last_err = TransientError(f"TikHub upstream error (code={code}): {msg}")
                    if attempt < self._max_retries:
                        time.sleep(self._backoff_base * (2**attempt))
                        continue
                    raise last_err
                raise ProviderError(f"TikHub API error (code={code}) on {endpoint}: {msg}")

            return payload

        raise last_err or TransientError(f"Request to {endpoint} failed after retries.")

    @staticmethod
    def _unwrap_data(payload: dict[str, Any]) -> Any:
        data = payload.get("data")
        return data if data is not None else payload

    def fetch_user_info(self, user_url_token: str) -> UserProfile | None:
        payload = self._request_json("fetch_user_info", {"user_url_token": user_url_token})
        data = self._unwrap_data(payload)
        if not isinstance(data, dict):
            return None
        if isinstance(data.get("data"), dict):
            data = data["data"]
        return UserProfile(
            url_token=str(data.get("url_token") or user_url_token),
            member_id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            articles_count=data.get("articles_count") if isinstance(data.get("articles_count"), int) else None,
            pins_count=data.get("pins_count") if isinstance(data.get("pins_count"), int) else None,
        )

    def _parse_list_response(
        self,
        payload: dict[str, Any],
        *,
        item_parser: Any,
        requested_offset: int,
        requested_limit: int,
    ) -> PageResult:
        data = self._unwrap_data(payload)
        if not isinstance(data, dict):
            raise ProviderError("Expected object in TikHub list response 'data'.")

        raw_items = data.get("data")
        if not isinstance(raw_items, list):
            raw_items = data.get("items") if isinstance(data.get("items"), list) else []

        paging = data.get("paging") if isinstance(data.get("paging"), dict) else {}
        is_end = bool(paging.get("is_end")) if "is_end" in paging else len(raw_items) == 0

        api_total = paging.get("totals")
        if not isinstance(api_total, int):
            api_total = None

        next_url = paging.get("next") if isinstance(paging.get("next"), str) else None
        next_offset = _extract_offset_from_url(next_url)
        if next_offset is None and not is_end:
            step = len(raw_items) if raw_items else requested_limit
            next_offset = requested_offset + step

        items = [item_parser(raw, from_detail=False) for raw in raw_items if isinstance(raw, dict)]
        return PageResult(
            items=items,
            is_end=is_end,
            next_offset=next_offset,
            next_cursor=next_url,
            api_total=api_total,
            raw_count=len(raw_items),
            raw_paging=paging,
        )

    def fetch_user_articles(
        self,
        user_url_token: str,
        offset: int = 0,
        limit: int = 20,
        cursor: str | None = None,
    ) -> PageResult:
        payload = self._request_json(
            "fetch_user_articles",
            {
                "user_url_token": user_url_token,
                "offset": str(offset),
                "limit": str(limit),
                "sort_type": "created",
            },
        )
        return self._parse_list_response(
            payload,
            item_parser=parse_article_dict,
            requested_offset=offset,
            requested_limit=limit,
        )

    def fetch_user_pins(
        self,
        user_url_token: str,
        offset: int = 0,
        limit: int = 20,
        cursor: str | None = None,
    ) -> PageResult:
        payload = self._request_json(
            "fetch_user_pins",
            {
                "user_url_token": user_url_token,
                "offset": str(offset),
                "limit": str(limit),
            },
        )
        return self._parse_list_response(
            payload,
            item_parser=parse_pin_dict,
            requested_offset=offset,
            requested_limit=limit,
        )

    def fetch_article_detail(self, article_id: str) -> ArchiveItem:
        payload = self._request_json("fetch_column_article_detail", {"article_id": str(article_id)})
        data = self._unwrap_data(payload)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            raise ProviderError(f"Unexpected article detail response for {article_id}")
        return parse_article_dict(data, from_detail=True)

    def fetch_pin_detail(self, pin_id: str) -> ArchiveItem:
        payload = self._request_json("fetch_pin_detail", {"pin_id": str(pin_id)})
        data = self._unwrap_data(payload)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            raise ProviderError(f"Unexpected pin detail response for {pin_id}")
        return parse_pin_dict(data, from_detail=True)
