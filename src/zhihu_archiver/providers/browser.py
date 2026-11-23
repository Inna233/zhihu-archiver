from __future__ import annotations

from collections import deque
import json
import logging
import os
from pathlib import Path
import random
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from zhihu_archiver.models import ArchiveItem, PageResult, UserProfile
from zhihu_archiver.providers.base import (
    AuthError,
    ProviderError,
    RateLimitError,
    ZhihuProvider,
)
from zhihu_archiver.providers.tikhub import (
    _extract_offset_from_url,
    parse_answer_dict,
    parse_article_dict,
    parse_pin_dict,
    parse_question_dict,
)

logger = logging.getLogger(__name__)


def resolve_chrome_cdp_url(cdp_url: str) -> str:
    """Resolve 'auto' by reading Chrome's DevToolsActivePort file, or return cdp_url as-is."""
    if cdp_url.strip().lower() != "auto":
        return cdp_url.strip()

    candidates: list[Path] = []
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            candidates.append(Path(local_app) / "Google" / "Chrome" / "User Data" / "DevToolsActivePort")
            candidates.append(Path(local_app) / "Microsoft" / "Edge" / "User Data" / "DevToolsActivePort")
    elif sys.platform == "darwin":
        home = Path.home()
        candidates.append(home / "Library" / "Application Support" / "Google" / "Chrome" / "DevToolsActivePort")
        candidates.append(home / "Library" / "Application Support" / "Microsoft Edge" / "DevToolsActivePort")
    else:
        home = Path.home()
        candidates.append(home / ".config" / "google-chrome" / "DevToolsActivePort")
        candidates.append(home / ".config" / "chromium" / "DevToolsActivePort")

    for path in candidates:
        if path.is_file():
            lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if len(lines) >= 2 and lines[0].isdigit():
                port, ws_path = lines[0], lines[1]
                return f"ws://127.0.0.1:{port}{ws_path}"

    raise ProviderError(
        "Could not auto-discover Chrome DevToolsActivePort. "
        "Enable remote debugging at chrome://inspect/#remote-debugging or pass an explicit --cdp-url."
    )


def _extract_initial_data(page: Any) -> dict[str, Any] | None:
    """Read Zhihu's SSR <script id='js-initialData'> JSON blob from the current page."""
    try:
        raw = page.evaluate(
            """() => {
                const el = document.getElementById('js-initialData');
                return el ? el.textContent : null;
            }"""
        )
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to parse js-initialData: %s", exc)
    return None


def _check_anti_bot_url(url: str) -> None:
    if "/account/unhuman" in url or "unhuman" in url:
        raise RateLimitError(
            "Zhihu triggered security verification (/account/unhuman). "
            "Stopped immediately without bypassing CAPTCHA. "
            "Please wait or verify manually in the browser before resuming."
        )


def _check_api_error_payload(status: int, payload: Any, endpoint_desc: str) -> None:
    if status == 401:
        raise AuthError(f"Zhihu returned HTTP 401 on {endpoint_desc}. Login required.")
    if status == 403:
        code = None
        msg = ""
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            code = payload["error"].get("code")
            msg = str(payload["error"].get("message") or "")
        if code == 40352:
            raise RateLimitError(
                f"Zhihu risk control triggered (code=40352) on {endpoint_desc}: {msg}. "
                "Stopped immediately; please slow down or verify in browser."
            )
        raise AuthError(f"Zhihu returned HTTP 403 on {endpoint_desc}: {msg}")
    if status == 429:
        raise RateLimitError(f"Zhihu rate limited (HTTP 429) on {endpoint_desc}.")


class BrowserProvider(ZhihuProvider):
    """Free Zhihu provider using a real browser (Playwright / Chrome CDP) + network response interception.

    Avoids reverse-engineering x-zse-96 by letting Zhihu's own web frontend compute signatures
    while intercepting the official /api/v4/ JSON responses at conservative, human-like pacing.
    """

    name = "browser"

    def __init__(
        self,
        *,
        profile_dir: Path | str = ".browser_profile",
        cdp_url: str | None = None,
        channel: str = "chrome",
        headless: bool = False,
        min_delay: float = 2.5,
        max_delay: float = 5.0,
        wait_for_login: bool = True,
        login_timeout: int = 180,
        nav_timeout_ms: int = 30000,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.cdp_url = cdp_url
        self.channel = channel
        self.headless = headless
        self.min_delay = max(0.0, min_delay)
        self.max_delay = max(self.min_delay, max_delay)
        self.wait_for_login = wait_for_login
        self.login_timeout = login_timeout
        self.nav_timeout_ms = nav_timeout_ms

        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._has_navigated = False
        self._active_list_key: tuple[str, str, str] | None = None
        self._active_listener: Any = None
        self._captured_queue: deque[tuple[int, str, Any]] = deque()
        self._seen_resp_urls: set[str] = set()
        self._page_param_unsupported: set[tuple[str, str, str]] = set()

    def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ProviderError(
                "Playwright is not installed. Run `uv sync` to install it."
            ) from exc

        self._pw = sync_playwright().start()

        if self.cdp_url:
            resolved_cdp = resolve_chrome_cdp_url(self.cdp_url)
            logger.info("Connecting to existing browser via CDP: %s", resolved_cdp)
            self._browser = self._pw.chromium.connect_over_cdp(resolved_cdp, timeout=90000)
            if self._browser.contexts:
                scored: list[tuple[int, int, Any]] = []
                for idx, ctx in enumerate(self._browser.contexts):
                    score = 0
                    try:
                        if any("zhihu.com" in (pg.url or "") for pg in ctx.pages):
                            score += 2
                        cookies = ctx.cookies("https://www.zhihu.com")
                        if any(c.get("name") == "z_c0" and c.get("value") for c in cookies):
                            score += 4
                    except Exception:  # noqa: BLE001
                        pass
                    scored.append((score, -idx, ctx))
                scored.sort(reverse=True)
                best_score, neg_idx, chosen_ctx = scored[0]
                logger.info(
                    "Selected CDP context #%d (score=%d, total_contexts=%d)",
                    -neg_idx,
                    best_score,
                    len(self._browser.contexts),
                )
                self._context = chosen_ctx
            else:
                self._context = self._browser.new_context()
            self._page = self._context.new_page()
        else:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            resolved_profile = str(self.profile_dir.resolve())
            logger.info(
                "Launching persistent %s browser (profile: %s, headless=%s)",
                self.channel,
                resolved_profile,
                self.headless,
            )
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=resolved_profile,
                channel=self.channel,
                headless=self.headless,
                no_viewport=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--window-position=120,120",
                    "--window-size=1280,860",
                ],
            )
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )

        self._page.set_default_timeout(self.nav_timeout_ms)
        return self._page

    def _detach_list_listener(self) -> None:
        if self._page is not None and self._active_listener is not None:
            try:
                self._page.remove_listener("response", self._active_listener)
            except Exception:  # noqa: BLE001
                pass
        self._active_listener = None
        self._active_list_key = None
        self._captured_queue.clear()
        self._seen_resp_urls.clear()

    def close(self) -> None:
        self._detach_list_listener()
        try:
            if self.cdp_url and self._page is not None:
                self._page.close()
            elif self._context is not None:
                self._context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None

    def _polite_sleep(self) -> None:
        if self._has_navigated and self.max_delay > 0:
            delay = random.uniform(self.min_delay, self.max_delay)
            logger.info("Polite delay: sleeping %.1fs before next page...", delay)
            time.sleep(delay)
        self._has_navigated = True

    def _has_login_cookie(self) -> bool:
        if self._context is None:
            return False
        try:
            cookies = self._context.cookies("https://www.zhihu.com")
            return any(c.get("name") == "z_c0" and c.get("value") for c in cookies)
        except Exception:  # noqa: BLE001
            return False

    def _ensure_logged_in(self, page: Any) -> None:
        if self._has_login_cookie():
            return
        # If we already have an active Zhihu list tab loaded and not redirected to /signin,
        # avoid disrupting it on transient cookie query hiccups.
        if self._active_list_key is not None and "/signin" not in (page.url or ""):
            return
        if self.headless or not self.wait_for_login:
            logger.warning("No z_c0 login cookie detected; proceeding as guest (may hit login wall).")
            return

        self._detach_list_listener()
        logger.info("No Zhihu login session found. Opening Zhihu sign-in page...")
        print(
            "\n[需要登录] 请在刚打开的 Chrome 窗口中登录知乎（扫码即可），"
            "登录完成后脚本会自动继续（不会接管你的鼠标键盘）。\n",
            flush=True,
        )
        page.goto("https://www.zhihu.com/signin", wait_until="domcontentloaded")
        try:
            page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        deadline = time.time() + self.login_timeout
        while time.time() < deadline:
            if self._has_login_cookie():
                logger.info("Zhihu login detected! Continuing...")
                print("[登录成功] 已检测到知乎登录态，继续执行...\n", flush=True)
                time.sleep(1.5)
                return
            time.sleep(1.0)
        raise AuthError(
            f"Timed out after {self.login_timeout}s waiting for Zhihu login in browser."
        )

    def fetch_user_info(self, user_url_token: str) -> UserProfile | None:
        page = self._ensure_page()
        self._ensure_logged_in(page)
        self._polite_sleep()

        captured_member: dict[str, Any] | None = None

        def on_response(resp: Any) -> None:
            nonlocal captured_member
            url = resp.url
            if f"/api/v4/members/{user_url_token}" in url and "/articles" not in url and "/pins" not in url:
                try:
                    if resp.status == 200:
                        data = resp.json()
                        if isinstance(data, dict) and data.get("url_token"):
                            captured_member = data
                except Exception:  # noqa: BLE001
                    pass

        page.on("response", on_response)
        try:
            page.goto(
                f"https://www.zhihu.com/people/{user_url_token}",
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(2000)
            _check_anti_bot_url(page.url)
        finally:
            page.remove_listener("response", on_response)

        user_data: dict[str, Any] | None = captured_member
        if not user_data:
            init_data = _extract_initial_data(page)
            if init_data:
                users = (
                    init_data.get("initialState", {})
                    .get("entities", {})
                    .get("users", {})
                )
                if isinstance(users, dict):
                    candidate = users.get(user_url_token)
                    if isinstance(candidate, dict):
                        user_data = candidate

        if not user_data:
            return None

        def _int_or_none(val: Any) -> int | None:
            return val if isinstance(val, int) else None

        return UserProfile(
            url_token=str(user_data.get("url_token") or user_url_token),
            member_id=str(user_data.get("id") or ""),
            name=str(user_data.get("name") or ""),
            articles_count=_int_or_none(
                user_data.get("articlesCount")
                if "articlesCount" in user_data
                else user_data.get("articles_count")
            ),
            pins_count=_int_or_none(
                user_data.get("pinsCount")
                if "pinsCount" in user_data
                else user_data.get("pins_count")
            ),
            answers_count=_int_or_none(
                user_data.get("answerCount")
                if "answerCount" in user_data
                else user_data.get("answer_count")
            ),
            questions_count=_int_or_none(
                user_data.get("questionCount")
                if "questionCount" in user_data
                else user_data.get("question_count")
            ),
            follower_count=_int_or_none(
                user_data.get("followerCount")
                if "followerCount" in user_data
                else user_data.get("follower_count")
            ),
        )

    def _fetch_list_via_page(
        self,
        *,
        user_url_token: str,
        tab: str,
        api_substrings: tuple[str, ...],
        entity_key: str,
        item_parser: Any,
        offset: int,
        limit: int,
        extra_query: str = "",
        try_page_param: bool = False,
    ) -> PageResult:
        page = self._ensure_page()
        self._ensure_logged_in(page)

        list_key = (tab, user_url_token, extra_query)
        base_url = f"https://www.zhihu.com/people/{user_url_token}/{tab}"

        def _build_url(page_num: int = 1) -> str:
            parts: list[str] = []
            if extra_query:
                parts.append(extra_query)
            if page_num > 1:
                parts.append(f"page={page_num}")
            return f"{base_url}?{'&'.join(parts)}" if parts else base_url

        # If try_page_param is enabled and offset > 0, check if we can jump directly via ?page=N
        # (works even when the tab is in the background, and resumes batches instantly without re-scrolling)
        if (
            try_page_param
            and offset > 0
            and not self._captured_queue
            and list_key not in self._page_param_unsupported
        ):
            page_num = (offset // max(1, limit)) + 1
            jump_url = _build_url(page_num)
            self._detach_list_listener()
            self._polite_sleep()

            def on_jump_response(resp: Any) -> None:
                url = resp.url
                if any(sub in url for sub in api_substrings):
                    if url in self._seen_resp_urls:
                        return
                    self._seen_resp_urls.add(url)
                    try:
                        body = resp.json()
                    except Exception:  # noqa: BLE001
                        body = None
                    self._captured_queue.append((resp.status, url, body))

            self._active_listener = on_jump_response
            self._active_list_key = list_key
            page.on("response", on_jump_response)
            logger.info("Navigating directly to %s (target offset=%d)", jump_url, offset)
            page.goto(jump_url, wait_until="domcontentloaded")
            deadline = time.time() + 10.0
            while time.time() < deadline and not self._captured_queue:
                _check_anti_bot_url(page.url)
                page.wait_for_timeout(400)
            if self._captured_queue:
                page.wait_for_timeout(600)
            _check_anti_bot_url(page.url)
            if "/signin" in page.url:
                self._detach_list_listener()
                raise AuthError(f"Redirected to Zhihu sign-in page when visiting {jump_url}.")

            # Verify whether the jumped page actually returned offset >= requested offset
            matched_jump = False
            for i, (_, r_url, _) in enumerate(self._captured_queue):
                r_off = _extract_offset_from_url(r_url)
                if r_off is not None and r_off >= offset:
                    for _ in range(i):
                        self._captured_queue.popleft()
                    matched_jump = True
                    break
            if not matched_jump:
                # Zhihu ignored ?page=N on this tab; remember and fall back to scrolling
                self._page_param_unsupported.add(list_key)
                logger.info("Direct ?page=%d did not jump to offset=%d; falling back to scroll.", page_num, offset)
                end_reached = self._fast_forward_scroll(page, tab=tab, offset=offset, limit=limit)
                if end_reached:
                    return PageResult(items=[], is_end=True, next_offset=offset, raw_count=0)
        elif self._active_list_key != list_key:
            self._detach_list_listener()
            self._polite_sleep()

            def on_response(resp: Any) -> None:
                url = resp.url
                if any(sub in url for sub in api_substrings):
                    if url in self._seen_resp_urls:
                        return
                    self._seen_resp_urls.add(url)
                    try:
                        body = resp.json()
                    except Exception:  # noqa: BLE001
                        body = None
                    self._captured_queue.append((resp.status, url, body))

            self._active_listener = on_response
            self._active_list_key = list_key
            page.on("response", on_response)

            target_url = _build_url(1)
            logger.info("Navigating to %s (starting offset=%d)", target_url, offset)
            page.goto(target_url, wait_until="domcontentloaded")
            deadline = time.time() + 10.0
            while time.time() < deadline and not self._captured_queue:
                _check_anti_bot_url(page.url)
                page.wait_for_timeout(400)
            if self._captured_queue:
                page.wait_for_timeout(600)
            _check_anti_bot_url(page.url)

            if "/signin" in page.url:
                self._detach_list_listener()
                raise AuthError(f"Redirected to Zhihu sign-in page when visiting {target_url}.")

            if offset > 0:
                end_reached = self._fast_forward_scroll(page, tab=tab, offset=offset, limit=limit)
                if end_reached:
                    return PageResult(items=[], is_end=True, next_offset=offset, raw_count=0)
        else:
            # Guard: ensure the bound tab is still on the target user's page before scrolling
            if f"/people/{user_url_token}" not in (page.url or ""):
                self._detach_list_listener()
                raise ProviderError(
                    f"Bound tab navigated away from /people/{user_url_token}/{tab} "
                    f"(current URL: {page.url}). Aborting scroll safely."
                )
            # Same list tab: scroll to trigger the next infinite-scroll API request if queue is empty
            if not self._captured_queue:
                self._polite_sleep()
                for scroll_attempt in range(3):
                    logger.info(
                        "Scrolling %s to load next page (expected offset=%d, attempt=%d)",
                        tab,
                        offset,
                        scroll_attempt + 1,
                    )
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    deadline = time.time() + 6.0
                    while time.time() < deadline and not self._captured_queue:
                        _check_anti_bot_url(page.url)
                        page.wait_for_timeout(350)
                    if self._captured_queue:
                        page.wait_for_timeout(400)
                        break
                    page.evaluate("window.scrollBy(0, -300)")
                    page.wait_for_timeout(300)

        if self._captured_queue:
            status, resp_url, payload = self._captured_queue.popleft()
            _check_api_error_payload(status, payload, f"{tab} (offset={offset})")
            if not isinstance(payload, dict):
                raise ProviderError(f"Non-JSON response from {resp_url}")

            raw_items = payload.get("data") if isinstance(payload.get("data"), list) else []
            paging = payload.get("paging") if isinstance(payload.get("paging"), dict) else {}
            is_end = bool(paging.get("is_end")) if "is_end" in paging else len(raw_items) == 0
            api_total = paging.get("totals") if isinstance(paging.get("totals"), int) else None
            next_url = paging.get("next") if isinstance(paging.get("next"), str) else None
            next_offset = _extract_offset_from_url(next_url)
            if next_offset is None and not is_end:
                next_offset = offset + (len(raw_items) if raw_items else limit)

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

        # If we already fetched at least one page and scrolling triggered nothing more,
        # do NOT blindly claim is_end=True (could be background tab throttling or page navigation);
        # raise ProviderError so checkpoint stays resumable instead of falsely completing.
        if offset > 0:
            raise ProviderError(
                f"Scroll did not trigger next API page for {tab} at offset={offset} "
                f"(current URL: {page.url}). Stopped cleanly so you can resume."
            )

        # Fallback for offset=0: SSR js-initialData
        init_data = _extract_initial_data(page)
        if init_data:
            entities = (
                init_data.get("initialState", {})
                .get("entities", {})
                .get(entity_key, {})
            )
            if isinstance(entities, dict) and entities:
                raw_items = [v for v in entities.values() if isinstance(v, dict)]
                items = [item_parser(raw, from_detail=False) for raw in raw_items]
                return PageResult(
                    items=items,
                    is_end=len(items) < limit,
                    next_offset=offset + len(items),
                    api_total=None,
                    raw_count=len(raw_items),
                    raw_paging={"source": "js-initialData"},
                )

        raise ProviderError(
            f"No API response or initialData captured for {_build_url(1)}. "
            "Zhihu may require login or the page structure changed."
        )

    def _fast_forward_scroll(self, page: Any, *, tab: str, offset: int, limit: int) -> bool:
        """Scroll forward until captured queue reaches target offset. Returns True if list ended."""
        max_ff_scrolls = max(10, (offset // max(1, limit)) + 10)
        for _ in range(max_ff_scrolls):
            matched_idx: int | None = None
            reached_end_ff = False
            for i, (_, r_url, r_body) in enumerate(self._captured_queue):
                r_off = _extract_offset_from_url(r_url)
                if r_off is not None and r_off >= offset:
                    matched_idx = i
                    break
                if isinstance(r_body, dict) and isinstance(r_body.get("paging"), dict):
                    if r_body["paging"].get("is_end"):
                        reached_end_ff = True
            if matched_idx is not None:
                for _ in range(matched_idx):
                    self._captured_queue.popleft()
                return False
            if reached_end_ff:
                self._captured_queue.clear()
                return True
            self._captured_queue.clear()
            # Use a gentler fast-forward delay (capped at 1.5-2.5s) while catching up to checkpoint
            ff_delay = random.uniform(min(1.5, self.min_delay), min(2.5, self.max_delay))
            if ff_delay > 0:
                time.sleep(ff_delay)
            logger.info("Fast-forwarding %s scroll to resume at offset=%d...", tab, offset)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            deadline = time.time() + 6.0
            while time.time() < deadline and not self._captured_queue:
                _check_anti_bot_url(page.url)
                page.wait_for_timeout(350)
            if not self._captured_queue:
                # One extra scroll nudge before giving up
                page.evaluate("window.scrollBy(0, -300)")
                page.wait_for_timeout(300)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                deadline2 = time.time() + 5.0
                while time.time() < deadline2 and not self._captured_queue:
                    _check_anti_bot_url(page.url)
                    page.wait_for_timeout(350)
                if not self._captured_queue:
                    break
        return False

    def fetch_user_articles(
        self,
        user_url_token: str,
        offset: int = 0,
        limit: int = 20,
        cursor: str | None = None,
    ) -> PageResult:
        return self._fetch_list_via_page(
            user_url_token=user_url_token,
            tab="posts",
            api_substrings=(f"/api/v4/members/{user_url_token}/articles",),
            entity_key="articles",
            item_parser=parse_article_dict,
            offset=offset,
            limit=limit,
        )

    def fetch_user_pins(
        self,
        user_url_token: str,
        offset: int = 0,
        limit: int = 20,
        cursor: str | None = None,
    ) -> PageResult:
        return self._fetch_list_via_page(
            user_url_token=user_url_token,
            tab="pins",
            api_substrings=(
                f"/api/v4/v2/pins/{user_url_token}/moments",
                f"/api/v4/members/{user_url_token}/pins",
            ),
            entity_key="pins",
            item_parser=parse_pin_dict,
            offset=offset,
            limit=limit,
        )

    def fetch_user_answers(
        self,
        user_url_token: str,
        offset: int = 0,
        limit: int = 20,
        sort_by: str = "created",
        cursor: str | None = None,
    ) -> PageResult:
        extra = f"sort_by={sort_by}" if sort_by and sort_by != "created" else ""
        return self._fetch_list_via_page(
            user_url_token=user_url_token,
            tab="answers",
            api_substrings=(f"/api/v4/members/{user_url_token}/answers",),
            entity_key="answers",
            item_parser=parse_answer_dict,
            offset=offset,
            limit=limit,
            extra_query=extra,
            try_page_param=False,
        )

    def fetch_user_questions(
        self,
        user_url_token: str,
        offset: int = 0,
        limit: int = 20,
        cursor: str | None = None,
    ) -> PageResult:
        return self._fetch_list_via_page(
            user_url_token=user_url_token,
            tab="asks",
            api_substrings=(f"/api/v4/members/{user_url_token}/questions",),
            entity_key="questions",
            item_parser=parse_question_dict,
            offset=offset,
            limit=limit,
        )

    def fetch_answer_detail(self, answer_id: str) -> ArchiveItem:
        self._detach_list_listener()
        page = self._ensure_page()
        self._ensure_logged_in(page)
        self._polite_sleep()

        target_url = f"https://www.zhihu.com/answer/{answer_id}"
        page.goto(target_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        _check_anti_bot_url(page.url)

        init_data = _extract_initial_data(page)
        if init_data:
            answers = (
                init_data.get("initialState", {})
                .get("entities", {})
                .get("answers", {})
            )
            if isinstance(answers, dict) and str(answer_id) in answers:
                return parse_answer_dict(answers[str(answer_id)], from_detail=True)

        raise ProviderError(f"Could not extract full content for answer {answer_id} from {target_url}")

    def fetch_article_detail(self, article_id: str) -> ArchiveItem:
        self._detach_list_listener()
        page = self._ensure_page()
        self._ensure_logged_in(page)
        self._polite_sleep()

        target_url = f"https://zhuanlan.zhihu.com/p/{article_id}"
        page.goto(target_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        _check_anti_bot_url(page.url)

        init_data = _extract_initial_data(page)
        if init_data:
            articles = (
                init_data.get("initialState", {})
                .get("entities", {})
                .get("articles", {})
            )
            if isinstance(articles, dict) and str(article_id) in articles:
                return parse_article_dict(articles[str(article_id)], from_detail=True)

        # Fallback: extract DOM title + rich text HTML
        dom_data = page.evaluate(
            """() => {
                const titleEl = document.querySelector('h1.Post-Title');
                const bodyEl = document.querySelector('.Post-RichText');
                return {
                    title: titleEl ? titleEl.textContent.trim() : '',
                    content: bodyEl ? bodyEl.innerHTML : ''
                };
            }"""
        )
        if dom_data and dom_data.get("content"):
            return parse_article_dict(
                {
                    "id": str(article_id),
                    "title": dom_data.get("title", ""),
                    "url": target_url,
                    "content": dom_data.get("content", ""),
                },
                from_detail=True,
            )

        raise ProviderError(f"Could not extract full content for article {article_id} from {target_url}")

    def fetch_pin_detail(self, pin_id: str) -> ArchiveItem:
        self._detach_list_listener()
        page = self._ensure_page()
        self._ensure_logged_in(page)
        self._polite_sleep()

        target_url = f"https://www.zhihu.com/pin/{pin_id}"
        captured: list[tuple[int, Any]] = []

        def on_response(resp: Any) -> None:
            if urlparse(resp.url).path.rstrip("/") == f"/api/v4/pins/{pin_id}":
                try:
                    captured.append((resp.status, resp.json()))
                except Exception:  # noqa: BLE001
                    pass

        page.on("response", on_response)
        try:
            page.goto(target_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            _check_anti_bot_url(page.url)
        finally:
            page.remove_listener("response", on_response)

        if captured:
            status, payload = captured[-1]
            _check_api_error_payload(status, payload, f"pin detail {pin_id}")
            if isinstance(payload, dict) and str(payload.get("id") or "") == str(pin_id):
                return parse_pin_dict(payload, from_detail=True)

        init_data = _extract_initial_data(page)
        if init_data:
            pins = (
                init_data.get("initialState", {})
                .get("entities", {})
                .get("pins", {})
            )
            if isinstance(pins, dict) and str(pin_id) in pins:
                return parse_pin_dict(pins[str(pin_id)], from_detail=True)

        raise ProviderError(f"Could not extract full content for pin {pin_id} from {target_url}")
