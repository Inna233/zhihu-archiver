from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from zhihu_archiver.pipeline import ArchiveConfig, ArchivePipeline
from zhihu_archiver.providers.tikhub import TikHubProvider
from zhihu_archiver.storage import ArchiveStore


def _make_article(
    aid: str,
    *,
    title: str,
    created: int,
    token: str = "xiao-chuan-1412",
    truncated: bool = False,
) -> dict[str, Any]:
    full_html = f"<p>这是文章 {aid} 的完整正文，包含论点与证据。</p><blockquote><p>引用来源 {aid}</p></blockquote>"
    return {
        "id": aid,
        "title": title,
        "url": f"https://api.zhihu.com/articles/{aid}",
        "author": {"id": "hash_author_001", "url_token": token, "name": "矢口"},
        "created": created,
        "updated": created + 3600,
        "excerpt": f"这是文章 {aid} 的摘要...",
        "content": "" if truncated else full_html,
        "title_image": f"https://picx.zhimg.com/cover-{aid}.jpg",
    }


def _make_pin(
    pid: str,
    *,
    created: int,
    token: str = "xiao-chuan-1412",
    with_quote: bool = False,
    missing_body: bool = False,
) -> dict[str, Any]:
    pin: dict[str, Any] = {
        "id": pid,
        "url": f"https://api.zhihu.com/pins/{pid}",
        "author": {"id": "hash_author_001", "url_token": token, "name": "矢口"},
        "created": created,
        "updated": created + 60,
        "excerpt_title": f"想法 {pid} 摘要...",
        "content": []
        if missing_body
        else [
            {"type": "text", "content": f"<p>作者自己对 {pid} 的看法。</p>"},
            {"type": "image", "original_url": f"https://picx.zhimg.com/pin-{pid}.png"},
        ],
    }
    if with_quote:
        pin["origin_pin"] = {
            "id": f"orig-{pid}",
            "type": "pin",
            "url": f"https://www.zhihu.com/pin/orig-{pid}",
            "author": {"id": "other_hash", "url_token": "other-user", "name": "原作者"},
            "created": created - 1000,
            "content": [{"type": "text", "content": "<p>这是被引用的原始想法正文。</p>"}],
        }
    return pin


def _build_mock_provider(handler: Any, api_key: str = "secret-test-key-999") -> TikHubProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return TikHubProvider(api_key=api_key, client=client, max_retries=1, backoff_base=0.01)


def test_sample_mode_and_full_resume_with_dedup_and_repost_separation(tmp_path: Path):
    articles_db = [
        _make_article("1001", title="第一篇文章", created=1700000100, truncated=True),
        _make_article("1002", title="第二篇文章", created=1700000200, truncated=False),
        _make_article("1003", title="第三篇文章", created=1700000300, truncated=False),
        _make_article("1004", title="第四篇文章", created=1700000400, truncated=True),
        # Mismatched author should be filtered out
        _make_article("9999", title="别人的文章", created=1700000500, token="someone-else"),
    ]
    pins_db = [
        _make_pin("2001", created=1700001100, with_quote=True),
        _make_pin("2002", created=1700001200, missing_body=True),
        _make_pin("2003", created=1700001300),
        _make_pin("2004", created=1700001400),
    ]

    detail_fail_once = {"2002": True}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params

        if path.endswith("/fetch_user_articles"):
            offset = int(params.get("offset", "0"))
            limit = int(params.get("limit", "2"))
            chunk = articles_db[offset : offset + limit]
            is_end = offset + limit >= len(articles_db)
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "data": chunk,
                        "paging": {
                            "is_end": is_end,
                            "totals": len(articles_db),
                            "next": f"https://www.zhihu.com/api/v4/members/xiao-chuan-1412/articles?offset={offset + limit}&limit={limit}",
                        },
                    },
                },
            )

        if path.endswith("/fetch_user_pins"):
            offset = int(params.get("offset", "0"))
            limit = int(params.get("limit", "2"))
            chunk = pins_db[offset : offset + limit]
            is_end = offset + limit >= len(pins_db)
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "data": chunk,
                        "paging": {
                            "is_end": is_end,
                            "totals": len(pins_db),
                            "next": f"https://www.zhihu.com/api/v4/members/xiao-chuan-1412/pins?offset={offset + limit}&limit={limit}",
                        },
                    },
                },
            )

        if path.endswith("/fetch_column_article_detail"):
            aid = params["article_id"]
            full = _make_article(aid, title=f"完整标题 {aid}", created=1700000100, truncated=False)
            return httpx.Response(200, json={"code": 200, "data": full})

        if path.endswith("/fetch_pin_detail"):
            pid = params["pin_id"]
            if detail_fail_once.get(pid):
                detail_fail_once[pid] = False
                return httpx.Response(500, json={"code": 500, "message": "temporary upstream error"})
            full = _make_pin(pid, created=1700001200, with_quote=(pid == "2001"), missing_body=False)
            return httpx.Response(200, json={"code": 200, "data": full})

        return httpx.Response(404, json={"code": 404, "message": "not found"})

    # 1) Run in sample mode (2 pages of limit=2 + 1 detail per type = 6 requests)
    store = ArchiveStore(tmp_path / "archive.db")
    provider = _build_mock_provider(handler)
    try:
        sample_cfg = ArchiveConfig(
            user_url_token="xiao-chuan-1412",
            output_dir=tmp_path,
            mode="sample",
            page_limit=2,
            sample_pages=2,
            max_requests=10,
        )
        sample_summary = ArchivePipeline(provider, store, sample_cfg).run()
        assert sample_summary.manifest["status"] == "sample"
        assert sample_summary.requests_used == 6
        assert sample_summary.sample_report is not None
        assert sample_summary.sample_report["lists"]["article"]["page_new_ids"] == [2, 2]
        assert sample_summary.sample_report["lists"]["pin"]["page_new_ids"] == [2, 2]
        assert sample_summary.sample_report["detail_cross_checks"]["article"]["list_appeared_truncated"] is True
        assert sample_summary.sample_report["detail_cross_checks"]["article"]["url"] == "https://zhuanlan.zhihu.com/p/1001"
        assert sample_summary.sample_report["detail_cross_checks"]["pin"]["has_quote"] is True

        # 2) Run in full mode: finishes remaining article page (offset=4), skips mismatched author 9999,
        #    backfills truncated article 1004, and retries/succeeds pin 2002 (after 1 transient 500).
        full_cfg = ArchiveConfig(
            user_url_token="xiao-chuan-1412",
            output_dir=tmp_path,
            mode="full",
            page_limit=2,
            max_requests=50,
        )
        full_summary = ArchivePipeline(provider, store, full_cfg).run()
        assert full_summary.manifest["status"] == "completed"
        assert full_summary.mismatched_author_count == 1
        assert full_summary.manifest["types"]["article"]["deduped_count"] == 4
        assert full_summary.manifest["types"]["article"]["full_content_count"] == 4
        assert full_summary.manifest["types"]["article"]["reached_end"] is True
        assert full_summary.manifest["types"]["pin"]["deduped_count"] == 4
        assert full_summary.manifest["types"]["pin"]["full_content_count"] == 4
        assert full_summary.manifest["types"]["pin"]["reached_end"] is True

        # 3) Rerun full mode -> 0 requests used, no duplicates
        rerun_summary = ArchivePipeline(provider, store, full_cfg).run()
        assert rerun_summary.requests_used == 0
        assert rerun_summary.manifest["types"]["article"]["deduped_count"] == 4
        assert rerun_summary.manifest["types"]["pin"]["deduped_count"] == 4
    finally:
        provider.close()
        store.close()

    # Verify exported files
    corpus_lines = (tmp_path / "corpus.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(corpus_lines) == 8
    records = [json.loads(line) for line in corpus_lines]
    ids = {(r["type"], r["id"]) for r in records}
    assert len(ids) == 8
    assert ("article", "9999") not in ids

    # Verify pin with quote keeps author commentary separate from quoted original
    pin_2001_md = next((tmp_path / "pins").glob("*_2001.md")).read_text(encoding="utf-8")
    assert "## 作者附言" in pin_2001_md
    assert "作者自己对 2001 的看法。" in pin_2001_md
    assert "## 引用 / 转发原文" in pin_2001_md
    assert "> 这是被引用的原始想法正文。" in pin_2001_md
    assert "https://www.zhihu.com/pin/2001" in pin_2001_md

    # Verify article Markdown has readable body & canonical URL
    art_1001_md = next((tmp_path / "articles").glob("*_1001_*.md")).read_text(encoding="utf-8")
    assert "https://zhuanlan.zhihu.com/p/1001" in art_1001_md
    assert "这是文章 1001 的完整正文" in art_1001_md


def test_duplicate_cursor_stops_cleanly(tmp_path: Path):
    same_page = [
        _make_article("1001", title="A", created=1700000100),
        _make_article("1002", title="B", created=1700000200),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/fetch_user_articles"):
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "data": same_page,
                        "paging": {"is_end": False, "totals": 100},
                    },
                },
            )
        return httpx.Response(
            200,
            json={"code": 200, "data": {"data": [], "paging": {"is_end": True, "totals": 0}}},
        )

    store = ArchiveStore(tmp_path / "archive.db")
    provider = _build_mock_provider(handler)
    try:
        cfg = ArchiveConfig(
            user_url_token="xiao-chuan-1412",
            output_dir=tmp_path,
            mode="full",
            page_limit=2,
            max_requests=10,
        )
        summary = ArchivePipeline(provider, store, cfg).run()
        assert summary.manifest["status"] == "partial"
        art_info = summary.manifest["types"]["article"]
        assert art_info["list_status"] == "stopped_error"
        assert "Duplicate page" in (art_info["stop_reason"] or "")
        assert art_info["deduped_count"] == 2
    finally:
        provider.close()
        store.close()


def test_overlapping_window_continues_past_overlap(tmp_path: Path):
    """Zhihu sometimes returns 40 items at offset=20 then 20 overlapping items at offset=40."""
    p1 = [_make_article("1", title="1", created=1700000001), _make_article("2", title="2", created=1700000002)]
    p2 = [
        _make_article("3", title="3", created=1700000003),
        _make_article("4", title="4", created=1700000004),
        _make_article("5", title="5", created=1700000005),
    ]
    # Overlapping subset of p2, but advancing next offset
    p3 = [_make_article("4", title="4", created=1700000004), _make_article("5", title="5", created=1700000005)]
    p4 = [_make_article("6", title="6", created=1700000006)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/fetch_user_articles"):
            off = int(request.url.params.get("offset", "0"))
            pages = {
                0: (p1, 2, False),
                2: (p2, 4, False),
                4: (p3, 6, False),
                6: (p4, 8, True),
            }
            data, nxt, end = pages[off]
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "data": data,
                        "paging": {"is_end": end, "totals": 6, "next": f"https://www.zhihu.com/api/v4/x?offset={nxt}"},
                    },
                },
            )
        return httpx.Response(
            200,
            json={"code": 200, "data": {"data": [], "paging": {"is_end": True, "totals": 0}}},
        )

    store = ArchiveStore(tmp_path / "archive.db")
    provider = _build_mock_provider(handler)
    try:
        cfg = ArchiveConfig(
            user_url_token="xiao-chuan-1412",
            output_dir=tmp_path,
            mode="full",
            page_limit=2,
            max_requests=10,
        )
        summary = ArchivePipeline(provider, store, cfg).run()
        assert summary.manifest["status"] == "completed"
        assert summary.manifest["types"]["article"]["deduped_count"] == 6
    finally:
        provider.close()
        store.close()


def test_auth_error_halts_without_leaking_key(tmp_path: Path):
    secret_key = "super-secret-tikhub-key-xyz"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "message": "Invalid API key"})

    store = ArchiveStore(tmp_path / "archive.db")
    provider = _build_mock_provider(handler, api_key=secret_key)
    try:
        cfg = ArchiveConfig(
            user_url_token="xiao-chuan-1412",
            output_dir=tmp_path,
            mode="sample",
            max_requests=10,
        )
        summary = ArchivePipeline(provider, store, cfg).run()
        assert summary.requests_used == 1
        assert "authentication" in summary.halted_reason.lower()
        manifest_text = (tmp_path / "manifest.json").read_text(encoding="utf-8")
        assert secret_key not in manifest_text
    finally:
        provider.close()
        store.close()
