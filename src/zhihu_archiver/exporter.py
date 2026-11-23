from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from zhihu_archiver.models import ArchiveItem, ItemType
from zhihu_archiver.storage import ArchiveStore

logger = logging.getLogger(__name__)

_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')

TYPE_DIRS: dict[ItemType, str] = {
    "article": "articles",
    "pin": "pins",
    "answer": "answers",
    "question": "questions",
}

TYPE_LABELS: dict[ItemType, str] = {
    "article": "文章 (Articles)",
    "pin": "想法 (Pins)",
    "answer": "回答 (Answers)",
    "question": "提问 (Questions)",
}


def _safe_slug(text: str, max_len: int = 48) -> str:
    cleaned = _INVALID_FILENAME_CHARS.sub("_", text.strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    return cleaned[:max_len]


def _date_prefix(iso_ts: str | None) -> str:
    if iso_ts and len(iso_ts) >= 10:
        return iso_ts[:10]
    return "undated"


def item_markdown_filename(item: ArchiveItem) -> str:
    date_part = _date_prefix(item.created_at)
    if item.type in {"article", "answer", "question"} and item.title:
        slug = _safe_slug(item.title)
        if slug:
            return f"{date_part}_{item.id}_{slug}.md"
    return f"{date_part}_{item.id}.md"


def _yaml_escape(value: str | None) -> str:
    if value is None:
        return "null"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


def _download_images_map(items: list[ArchiveItem], images_dir: Path) -> dict[str, str]:
    """Download unique remote image URLs to images_dir and return {url: relative_path}."""
    urls: list[str] = []
    seen: set[str] = set()
    for item in items:
        for u in item.images:
            if u and u.startswith(("http://", "https://")) and u not in seen:
                seen.add(u)
                urls.append(u)
        if item.quote:
            for u in item.quote.images:
                if u and u.startswith(("http://", "https://")) and u not in seen:
                    seen.add(u)
                    urls.append(u)

    if not urls:
        return {}

    images_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}

    with httpx.Client(
        timeout=20.0,
        follow_redirects=True,
        headers={
            "Referer": "https://www.zhihu.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
    ) as client:
        for url in urls:
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            ext = Path(urlparse(url).path).suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}:
                ext = ".jpg"
            filename = f"{digest}{ext}"
            dest = images_dir / filename
            rel = f"../images/{filename}"
            if dest.is_file() and dest.stat().st_size > 0:
                mapping[url] = rel
                continue
            try:
                resp = client.get(url)
                if resp.status_code == 200 and resp.content:
                    dest.write_bytes(resp.content)
                    mapping[url] = rel
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to download image %s: %s", url, exc)

    return mapping


def _rewrite_images(text: str, image_map: dict[str, str]) -> str:
    if not image_map or not text:
        return text
    out = text
    for remote, local in image_map.items():
        out = out.replace(f"]({remote})", f"]({local})")
    return out


def render_item_markdown(
    item: ArchiveItem,
    *,
    image_map: dict[str, str] | None = None,
) -> str:
    """Render an ArchiveItem into a readable Markdown document with YAML frontmatter."""
    img_map = image_map or {}
    lines: list[str] = [
        "---",
        f"type: {item.type}",
        f"id: {_yaml_escape(str(item.id))}",
        f"title: {_yaml_escape(item.title) if item.title else 'null'}",
        f"url: {_yaml_escape(item.url)}",
        f"author_id: {_yaml_escape(item.author_id)}",
        f"author_url_token: {_yaml_escape(item.author_url_token)}",
        f"author_name: {_yaml_escape(item.author_name)}",
        f"created_at: {_yaml_escape(item.created_at)}",
        f"updated_at: {_yaml_escape(item.updated_at)}",
        f"was_edited: {'true' if item.was_edited else 'false'}",
    ]
    if item.question_id or item.question_url:
        lines.append(f"question_id: {_yaml_escape(item.question_id)}")
        lines.append(f"question_url: {_yaml_escape(item.question_url)}")
    if item.voteup_count is not None:
        lines.append(f"voteup_count: {item.voteup_count}")
    if item.comment_count is not None:
        lines.append(f"comment_count: {item.comment_count}")
    lines.extend(
        [
            f"is_full_content: {'true' if item.is_full_content else 'false'}",
            f"detail_fetched: {'true' if item.detail_fetched else 'false'}",
            f"images_count: {len(item.images)}",
            "---",
            "",
        ]
    )

    body_md = _rewrite_images(item.content_markdown, img_map)

    if item.type == "article":
        heading = item.title or f"Article {item.id}"
        lines.append(f"# {heading}")
        lines.append("")
        lines.append(f"- 原文链接：{item.url}")
        if item.created_at:
            lines.append(f"- 发表时间：{item.created_at}")
        if item.updated_at:
            edited_note = "（有后续编辑）" if item.was_edited else ""
            lines.append(f"- 更新时间：{item.updated_at}{edited_note}")
        if item.voteup_count is not None:
            lines.append(f"- 赞同数：{item.voteup_count} | 评论数：{item.comment_count or 0}")
        lines.append("")
        lines.append(body_md or "*（正文为空）*")
    elif item.type == "answer":
        heading = item.question_title or item.title or f"Answer {item.id}"
        lines.append(f"# {heading}")
        lines.append("")
        lines.append(f"- 回答链接：{item.url}")
        if item.question_url:
            lines.append(f"- 原问题链接：{item.question_url}")
        if item.created_at:
            lines.append(f"- 首次回答时间：{item.created_at}")
        if item.updated_at:
            edited_note = "（后续有编辑/补充）" if item.was_edited else ""
            lines.append(f"- 最后编辑时间：{item.updated_at}{edited_note}")
        if item.voteup_count is not None:
            lines.append(f"- 赞同数：{item.voteup_count} | 评论数：{item.comment_count or 0}")
        lines.append("")
        lines.append(body_md or "*（正文为空）*")
    elif item.type == "question":
        heading = item.title or f"Question {item.id}"
        lines.append(f"# {heading}")
        lines.append("")
        lines.append(f"- 问题链接：{item.url}")
        if item.created_at:
            lines.append(f"- 提问时间：{item.created_at}")
        if item.updated_at:
            lines.append(f"- 更新时间：{item.updated_at}")
        if item.voteup_count is not None or item.comment_count is not None:
            lines.append(f"- 关注数：{item.voteup_count or 0} | 回答数：{item.comment_count or 0}")
        lines.append("")
        lines.append(body_md or "*（无补充问题描述）*")
    else:
        lines.append(f"# 想法 {item.id}")
        lines.append("")
        lines.append(f"- 原文链接：{item.url}")
        if item.created_at:
            lines.append(f"- 发表时间：{item.created_at}")
        if item.updated_at:
            lines.append(f"- 更新时间：{item.updated_at}")
        if item.voteup_count is not None:
            lines.append(f"- 点赞数：{item.voteup_count} | 评论数：{item.comment_count or 0}")
        lines.append("")
        if item.quote is not None:
            lines.append("## 作者附言")
            lines.append("")
            lines.append(body_md or "*（无附言，纯转发）*")
            lines.append("")
            lines.append("## 引用 / 转发原文")
            lines.append("")
            if item.quote.author_name or item.quote.author_url_token:
                author_label = item.quote.author_name or item.quote.author_url_token
                lines.append(f"- 原作者：{author_label} (`{item.quote.author_url_token}`)")
            if item.quote.url:
                lines.append(f"- 引用链接：{item.quote.url}")
            if item.quote.created_at:
                lines.append(f"- 引用发表时间：{item.quote.created_at}")
            lines.append("")
            q_md = _rewrite_images(item.quote.content_markdown, img_map)
            if q_md:
                for qline in q_md.splitlines():
                    lines.append(f"> {qline}" if qline else ">")
            else:
                lines.append("> *（引用正文为空）*")
            if item.quote.images:
                lines.append("")
                lines.append("### 引用图片")
                lines.append("")
                for img in item.quote.images:
                    lines.append(f"- {img}")
        else:
            lines.append(body_md or "*（正文为空）*")

    if item.images:
        lines.append("")
        lines.append("## 图片链接")
        lines.append("")
        for img in item.images:
            local = img_map.get(img)
            if local:
                lines.append(f"- {img} (`{local}`)")
            else:
                lines.append(f"- {img}")

    lines.append("")
    return "\n".join(lines)


def _render_index_markdown(
    store: ArchiveStore,
    items_by_type: dict[ItemType, list[ArchiveItem]],
) -> str:
    user_token = store.get_meta("user_url_token")
    author_name = store.get_meta("author_name") or user_token
    lines: list[str] = [
        f"# {author_name}（`{user_token}`）知乎归档索引",
        "",
        f"- 主页：https://www.zhihu.com/people/{user_token}",
        "",
    ]

    for item_type in ("article", "pin", "answer", "question"):
        t: ItemType = item_type  # type: ignore[assignment]
        items = items_by_type.get(t) or []
        if not items:
            continue
        dir_name = TYPE_DIRS[t]
        edited_count = sum(1 for it in items if it.was_edited)
        extra_note = f"（其中 {edited_count} 条有后续编辑/补充）" if edited_count else ""
        lines.append(f"## {TYPE_LABELS[t]} — 共 {len(items)} 条{extra_note}")
        lines.append("")
        lines.append("| 发表日期 | 后续编辑 | 赞同/点赞 | 标题 / 摘要 | 原文 |")
        lines.append("|---|---|---|---|---|")
        for it in items:
            date_str = _date_prefix(it.created_at)
            edited_str = f"已改 ({_date_prefix(it.updated_at)})" if it.was_edited else "-"
            votes = str(it.voteup_count) if it.voteup_count is not None else "-"
            raw_label = it.title or it.content_markdown.replace("\n", " ")[:48] or it.id
            label = raw_label.replace("|", "\\|").strip()
            fname = item_markdown_filename(it)
            lines.append(f"| {date_str} | {edited_str} | {votes} | [{label}]({dir_name}/{fname}) | [知乎]({it.url}) |")
        lines.append("")

    return "\n".join(lines)


def export_archive(
    store: ArchiveStore,
    output_dir: Path,
    *,
    run_mode: str = "full",
    item_types: tuple[ItemType, ...] | None = None,
    sample_report: dict[str, Any] | None = None,
    download_images: bool = False,
) -> dict[str, Any]:
    """Export SQLite archive into corpus.jsonl, Markdown folders, INDEX.md, and manifest.json."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    all_items = store.get_all_items()
    image_map = _download_images_map(all_items, out / "images") if download_images else {}

    active_types: list[ItemType] = list(item_types or ("article", "pin"))
    for it in all_items:
        if it.type not in active_types:
            active_types.append(it.type)

    # Ensure output dirs exist for all active types
    for t in active_types:
        (out / TYPE_DIRS[t]).mkdir(parents=True, exist_ok=True)

    corpus_path = out / "corpus.jsonl"
    items_by_type: dict[ItemType, list[ArchiveItem]] = {t: [] for t in active_types}

    with corpus_path.open("w", encoding="utf-8") as fh:
        for item in all_items:
            fh.write(json.dumps(item.to_corpus_dict(), ensure_ascii=False) + "\n")
            items_by_type.setdefault(item.type, []).append(item)
            target_dir = out / TYPE_DIRS[item.type]
            target_dir.mkdir(parents=True, exist_ok=True)
            md_path = target_dir / item_markdown_filename(item)
            md_path.write_text(
                render_item_markdown(item, image_map=image_map),
                encoding="utf-8",
            )

    index_path = out / "INDEX.md"
    index_path.write_text(_render_index_markdown(store, items_by_type), encoding="utf-8")

    types_summary: dict[str, Any] = {}
    all_completed = True
    any_failures = False

    for t in active_types:
        items = items_by_type.get(t, [])
        prog = store.get_progress(t)
        failures = store.get_unresolved_failures(t)
        if failures:
            any_failures = True

        full_items = [it for it in items if it.is_full_content]
        partial_items = [it for it in items if not it.is_full_content]
        edited_items = [it for it in items if it.was_edited]
        dates = sorted(it.created_at for it in items if it.created_at)

        if not prog.is_end or prog.status not in {"completed"} or failures or partial_items:
            all_completed = False

        types_summary[t] = {
            "api_total": prog.api_total,
            "enumerated_count": prog.enumerated_count,
            "deduped_count": len(items),
            "full_content_count": len(full_items),
            "edited_after_publish_count": len(edited_items),
            "missing_or_partial_count": len(partial_items),
            "earliest_created_at": dates[0] if dates else None,
            "latest_created_at": dates[-1] if dates else None,
            "reached_end": prog.is_end,
            "list_status": prog.status,
            "stop_reason": prog.stop_reason or None,
            "missing_items": [
                {"id": it.id, "title": it.title or None, "url": it.url}
                for it in partial_items
            ],
            "failures": [
                {
                    "kind": f["kind"],
                    "item_id": f["item_id"] or None,
                    "offset": f["offset"],
                    "url": f["url"] or None,
                    "error_type": f["error_type"],
                    "error_message": f["error_message"],
                    "attempts": f["attempts"],
                }
                for f in failures
            ],
        }

    if run_mode in {"sample", "preview"}:
        overall_status = run_mode
    elif all_completed and not any_failures:
        overall_status = "completed"
    else:
        overall_status = "partial"

    def _meta_int(key: str) -> int | None:
        raw = store.get_meta(key)
        return int(raw) if raw.isdigit() else None

    manifest: dict[str, Any] = {
        "user": {
            "url_token": store.get_meta("user_url_token"),
            "author_id": store.get_meta("author_id"),
            "author_name": store.get_meta("author_name"),
            "profile_url": f"https://www.zhihu.com/people/{store.get_meta('user_url_token')}",
            "profile_counts": {
                "articles": _meta_int("profile_articles_count"),
                "pins": _meta_int("profile_pins_count"),
                "answers": _meta_int("profile_answers_count"),
                "questions": _meta_int("profile_questions_count"),
                "followers": _meta_int("profile_follower_count"),
            },
        },
        "provider": store.get_meta("provider"),
        "mode": run_mode,
        "status": overall_status,
        "types": types_summary,
        "outputs": {
            "corpus_jsonl": str(corpus_path.name),
            "index_md": str(index_path.name),
            "dirs": {t: TYPE_DIRS[t] for t in active_types},
            "downloaded_images_count": len(image_map),
        },
    }
    if sample_report is not None:
        manifest["sample_report"] = sample_report

    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest
