from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

from zhihu_archiver.exporter import export_archive
from zhihu_archiver.models import parse_item_types, parse_user_url_token
from zhihu_archiver.pipeline import ArchiveConfig, ArchivePipeline
from zhihu_archiver.providers.base import AuthError, ZhihuProvider
from zhihu_archiver.providers.browser import BrowserProvider
from zhihu_archiver.providers.tikhub import TikHubProvider
from zhihu_archiver.storage import ArchiveStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zhihu-archiver",
        description="Archive a Zhihu user's public articles and pins to SQLite, JSONL, and Markdown.",
    )
    parser.add_argument(
        "user",
        help="Zhihu profile URL (e.g. https://www.zhihu.com/people/xiao-chuan-1412) or url_token.",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output directory for archive.db, corpus.jsonl, articles/, pins/, and manifest.json.",
    )
    parser.add_argument(
        "--mode",
        choices=["preview", "sample", "full"],
        default="sample",
        help="Run mode: 'preview' checks profile counts only; 'sample' fetches 2 pages + 1 detail per type (default); 'full' paginates to end.",
    )
    parser.add_argument(
        "--types",
        default="articles,pins",
        help="Comma-separated content types to archive: articles,pins,answers,questions (default: articles,pins).",
    )
    parser.add_argument(
        "--answer-sort",
        choices=["created", "voteups"],
        default="created",
        help="Sort order when fetching answers (default: created).",
    )
    parser.add_argument(
        "--download-images",
        action="store_true",
        help="Download referenced images into <out>/images and link them locally in Markdown.",
    )
    parser.add_argument(
        "--provider",
        choices=["browser", "tikhub"],
        default="browser",
        help="Data provider: 'browser' (free Playwright/Chrome interception, default) or 'tikhub' (requires TIKHUB_API_KEY).",
    )
    parser.add_argument(
        "--profile-dir",
        default=".browser_profile",
        help="Browser persistent profile directory for '--provider browser' (default: .browser_profile).",
    )
    parser.add_argument(
        "--cdp-url",
        default=None,
        help="Optional Chrome/Edge remote debugging URL (e.g. http://127.0.0.1:9222) for '--provider browser'.",
    )
    parser.add_argument(
        "--browser-channel",
        default="chrome",
        help="System browser channel for Playwright (default: chrome; can also use msedge).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (only recommended after login session is already saved).",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=2.5,
        help="Minimum random delay in seconds between browser page navigations (default: 2.5).",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=5.0,
        help="Maximum random delay in seconds between browser page navigations (default: 5.0).",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=20,
        help="Items requested per page (default: 20).",
    )
    parser.add_argument(
        "--sample-pages",
        type=int,
        default=2,
        help="Number of list pages per type in sample mode (default: 2).",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="Maximum API requests for this run (defaults: 10 in sample mode, 300 in full mode).",
    )
    parser.add_argument(
        "--fetch-user-info",
        action="store_true",
        help="Also call fetch_user_info once to record member_id and display name.",
    )
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="Re-export corpus.jsonl, Markdown files, and manifest.json from existing archive.db without API calls.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging.",
    )
    return parser


def _print_summary(manifest: dict, requests_used: int, halted_reason: str = "") -> None:
    print(f"\n=== Zhihu Archiver Summary ({manifest.get('mode')}) ===")
    user = manifest["user"]
    name_part = f" ({user.get('author_name')})" if user.get("author_name") else ""
    print(f"User: {user.get('url_token')}{name_part} -> {user.get('profile_url')}")
    counts = user.get("profile_counts") or {}
    if any(v is not None for v in counts.values()):
        print(
            f"Profile counts: articles={counts.get('articles')} | "
            f"pins={counts.get('pins')} | answers={counts.get('answers')} | "
            f"questions={counts.get('questions')} | followers={counts.get('followers')}"
        )
    print(f"Status: {manifest.get('status')} | Requests used this run: {requests_used}")
    if halted_reason:
        print(f"Halted reason: {halted_reason}")

    for item_type, info in manifest["types"].items():
        print(
            f"  [{item_type}] api_total={info['api_total']} | "
            f"enumerated={info['enumerated_count']} | "
            f"deduped={info['deduped_count']} | "
            f"full={info['full_content_count']} | "
            f"edited={info.get('edited_after_publish_count', 0)} | "
            f"missing/partial={info['missing_or_partial_count']} | "
            f"reached_end={info['reached_end']} ({info['list_status']})"
        )
        if info.get("stop_reason"):
            print(f"    stop_reason: {info['stop_reason']}")
        if info.get("failures"):
            print(f"    unresolved_failures: {len(info['failures'])}")

    if "sample_report" in manifest:
        print("\n--- Sample Report ---")
        print(json.dumps(manifest["sample_report"], indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        user_token = parse_user_url_token(args.user)
        item_types = parse_item_types(args.types)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "archive.db"
    store = ArchiveStore(db_path)

    try:
        if args.export_only:
            manifest = export_archive(
                store,
                out_dir,
                run_mode=args.mode,
                item_types=item_types,
                download_images=args.download_images,
            )
            _print_summary(manifest, requests_used=0)
            return 0

        max_requests = (
            args.max_requests
            if args.max_requests is not None
            else (2 if args.mode == "preview" else (10 if args.mode == "sample" else 300))
        )

        provider: ZhihuProvider
        try:
            if args.provider == "browser":
                provider = BrowserProvider(
                    profile_dir=args.profile_dir,
                    cdp_url=args.cdp_url,
                    channel=args.browser_channel,
                    headless=args.headless,
                    min_delay=args.min_delay,
                    max_delay=args.max_delay,
                )
            else:
                provider = TikHubProvider()
        except AuthError as exc:
            print(f"Authentication error: {exc}", file=sys.stderr)
            return 2

        try:
            config = ArchiveConfig(
                user_url_token=user_token,
                output_dir=out_dir,
                mode=args.mode,
                item_types=item_types,
                answer_sort=args.answer_sort,
                page_limit=args.page_limit,
                sample_pages=args.sample_pages,
                max_requests=max_requests,
                fetch_user_info=args.fetch_user_info,
                download_images=args.download_images,
            )
            pipeline = ArchivePipeline(provider=provider, store=store, config=config)
            summary = pipeline.run()
            _print_summary(
                summary.manifest,
                requests_used=summary.requests_used,
                halted_reason=summary.halted_reason,
            )
            return 0 if not summary.halted_reason else 1
        finally:
            provider.close()
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
