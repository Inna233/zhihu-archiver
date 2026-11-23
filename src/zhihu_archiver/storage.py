from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from zhihu_archiver.models import ArchiveItem, ItemType, QuoteContent


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class ProgressState:
    item_type: ItemType
    next_offset: int = 0
    next_cursor: str | None = None
    pages_fetched: int = 0
    enumerated_count: int = 0
    api_total: int | None = None
    is_end: bool = False
    status: str = "pending"
    last_page_ids: list[str] = None  # type: ignore[assignment]
    stop_reason: str = ""

    def __post_init__(self) -> None:
        if self.last_page_ids is None:
            self.last_page_ids = []


class ArchiveStore:
    """SQLite persistence for archived Zhihu items, pagination checkpoints, and failures."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS items (
                    type TEXT NOT NULL,
                    id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    author_id TEXT NOT NULL DEFAULT '',
                    author_url_token TEXT NOT NULL DEFAULT '',
                    author_name TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT,
                    content_markdown TEXT NOT NULL DEFAULT '',
                    excerpt TEXT NOT NULL DEFAULT '',
                    images_json TEXT NOT NULL DEFAULT '[]',
                    quote_json TEXT,
                    question_id TEXT NOT NULL DEFAULT '',
                    question_title TEXT NOT NULL DEFAULT '',
                    question_url TEXT NOT NULL DEFAULT '',
                    voteup_count INTEGER,
                    comment_count INTEGER,
                    content_blocks_count INTEGER NOT NULL DEFAULT 0,
                    is_full_content INTEGER NOT NULL DEFAULT 0,
                    needs_detail INTEGER NOT NULL DEFAULT 0,
                    detail_fetched INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    saved_at TEXT NOT NULL,
                    PRIMARY KEY (type, id)
                );

                CREATE TABLE IF NOT EXISTS progress (
                    item_type TEXT PRIMARY KEY,
                    next_offset INTEGER NOT NULL DEFAULT 0,
                    next_cursor TEXT,
                    pages_fetched INTEGER NOT NULL DEFAULT 0,
                    enumerated_count INTEGER NOT NULL DEFAULT 0,
                    api_total INTEGER,
                    is_end INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    last_page_ids_json TEXT NOT NULL DEFAULT '[]',
                    stop_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    item_id TEXT NOT NULL DEFAULT '',
                    offset INTEGER,
                    url TEXT NOT NULL DEFAULT '',
                    error_type TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    UNIQUE(kind, item_type, item_id, offset)
                );
                """
            )
            # Idempotent column migration for existing archive.db files
            existing_cols = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(items)").fetchall()
            }
            migrations = [
                ("question_id", "TEXT NOT NULL DEFAULT ''"),
                ("question_title", "TEXT NOT NULL DEFAULT ''"),
                ("question_url", "TEXT NOT NULL DEFAULT ''"),
                ("voteup_count", "INTEGER"),
                ("comment_count", "INTEGER"),
            ]
            for col_name, col_def in migrations:
                if col_name not in existing_cols:
                    self._conn.execute(f"ALTER TABLE items ADD COLUMN {col_name} {col_def}")

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def get_progress(self, item_type: ItemType) -> ProgressState:
        row = self._conn.execute(
            "SELECT * FROM progress WHERE item_type = ?", (item_type,)
        ).fetchone()
        if not row:
            return ProgressState(item_type=item_type)
        return ProgressState(
            item_type=item_type,
            next_offset=int(row["next_offset"]),
            next_cursor=row["next_cursor"],
            pages_fetched=int(row["pages_fetched"]),
            enumerated_count=int(row["enumerated_count"]),
            api_total=row["api_total"],
            is_end=bool(row["is_end"]),
            status=str(row["status"]),
            last_page_ids=json.loads(row["last_page_ids_json"] or "[]"),
            stop_reason=str(row["stop_reason"] or ""),
        )

    def _upsert_item_cursor(self, cur: sqlite3.Cursor, item: ArchiveItem, now: str) -> bool:
        """Insert or update an item. Returns True if newly inserted."""
        existing = cur.execute(
            "SELECT detail_fetched, is_full_content FROM items WHERE type = ? AND id = ?",
            (item.type, str(item.id)),
        ).fetchone()

        images_json = json.dumps(item.images, ensure_ascii=False)
        quote_json = json.dumps(item.quote.to_dict(), ensure_ascii=False) if item.quote else None
        raw_json = json.dumps(item.raw_json, ensure_ascii=False)

        if existing is None:
            cur.execute(
                """
                INSERT INTO items (
                    type, id, url, author_id, author_url_token, author_name,
                    title, created_at, updated_at, content_markdown, excerpt,
                    images_json, quote_json, question_id, question_title, question_url,
                    voteup_count, comment_count, content_blocks_count,
                    is_full_content, needs_detail, detail_fetched, raw_json, saved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.type,
                    str(item.id),
                    item.url,
                    item.author_id,
                    item.author_url_token,
                    item.author_name,
                    item.title,
                    item.created_at,
                    item.updated_at,
                    item.content_markdown,
                    item.excerpt,
                    images_json,
                    quote_json,
                    item.question_id,
                    item.question_title,
                    item.question_url,
                    item.voteup_count,
                    item.comment_count,
                    item.content_blocks_count,
                    int(item.is_full_content),
                    int(item.needs_detail),
                    int(item.detail_fetched),
                    raw_json,
                    now,
                ),
            )
            return True

        # Do not overwrite an already detail-fetched full item with a partial list item
        if existing["detail_fetched"] and existing["is_full_content"] and not item.detail_fetched:
            return False

        cur.execute(
            """
            UPDATE items SET
                url = CASE WHEN ? != '' THEN ? ELSE url END,
                author_id = CASE WHEN ? != '' THEN ? ELSE author_id END,
                author_url_token = CASE WHEN ? != '' THEN ? ELSE author_url_token END,
                author_name = CASE WHEN ? != '' THEN ? ELSE author_name END,
                title = CASE WHEN ? != '' THEN ? ELSE title END,
                created_at = COALESCE(?, created_at),
                updated_at = COALESCE(?, updated_at),
                content_markdown = ?,
                excerpt = CASE WHEN ? != '' THEN ? ELSE excerpt END,
                images_json = ?,
                quote_json = COALESCE(?, quote_json),
                question_id = CASE WHEN ? != '' THEN ? ELSE question_id END,
                question_title = CASE WHEN ? != '' THEN ? ELSE question_title END,
                question_url = CASE WHEN ? != '' THEN ? ELSE question_url END,
                voteup_count = COALESCE(?, voteup_count),
                comment_count = COALESCE(?, comment_count),
                content_blocks_count = MAX(content_blocks_count, ?),
                is_full_content = ?,
                needs_detail = ?,
                detail_fetched = MAX(detail_fetched, ?),
                raw_json = ?,
                saved_at = ?
            WHERE type = ? AND id = ?
            """,
            (
                item.url,
                item.url,
                item.author_id,
                item.author_id,
                item.author_url_token,
                item.author_url_token,
                item.author_name,
                item.author_name,
                item.title,
                item.title,
                item.created_at,
                item.updated_at,
                item.content_markdown,
                item.excerpt,
                item.excerpt,
                images_json,
                quote_json,
                item.question_id,
                item.question_id,
                item.question_title,
                item.question_title,
                item.question_url,
                item.question_url,
                item.voteup_count,
                item.comment_count,
                item.content_blocks_count,
                int(item.is_full_content),
                int(item.needs_detail),
                int(item.detail_fetched),
                raw_json,
                now,
                item.type,
                str(item.id),
            ),
        )
        return False

    def save_page_and_progress(
        self,
        item_type: ItemType,
        items: list[ArchiveItem],
        progress: ProgressState,
    ) -> int:
        """Atomically save a page of items and advance the pagination checkpoint."""
        now = _utc_now()
        new_count = 0
        with self._conn:
            cur = self._conn.cursor()
            for item in items:
                if self._upsert_item_cursor(cur, item, now):
                    new_count += 1
            cur.execute(
                """
                INSERT INTO progress (
                    item_type, next_offset, next_cursor, pages_fetched,
                    enumerated_count, api_total, is_end, status,
                    last_page_ids_json, stop_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_type) DO UPDATE SET
                    next_offset = excluded.next_offset,
                    next_cursor = excluded.next_cursor,
                    pages_fetched = excluded.pages_fetched,
                    enumerated_count = excluded.enumerated_count,
                    api_total = COALESCE(excluded.api_total, progress.api_total),
                    is_end = excluded.is_end,
                    status = excluded.status,
                    last_page_ids_json = excluded.last_page_ids_json,
                    stop_reason = excluded.stop_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    item_type,
                    progress.next_offset,
                    progress.next_cursor,
                    progress.pages_fetched,
                    progress.enumerated_count,
                    progress.api_total,
                    int(progress.is_end),
                    progress.status,
                    json.dumps(progress.last_page_ids, ensure_ascii=False),
                    progress.stop_reason,
                    now,
                ),
            )
            if progress.status in {"in_progress", "completed"} and items:
                cur.execute(
                    """
                    UPDATE failures SET resolved = 1, updated_at = ?
                    WHERE kind = 'list' AND item_type = ? AND resolved = 0
                    """,
                    (now, item_type),
                )
        return new_count

    def update_progress_status(
        self,
        item_type: ItemType,
        *,
        status: str,
        stop_reason: str = "",
        is_end: bool | None = None,
    ) -> None:
        prog = self.get_progress(item_type)
        prog.status = status
        prog.stop_reason = stop_reason
        if is_end is not None:
            prog.is_end = is_end
        self.save_page_and_progress(item_type, [], prog)

    def save_detail_item(self, item: ArchiveItem) -> None:
        """Save a detail-fetched item and mark any prior detail failure as resolved."""
        now = _utc_now()
        with self._conn:
            cur = self._conn.cursor()
            self._upsert_item_cursor(cur, item, now)
            cur.execute(
                """
                UPDATE failures SET resolved = 1, updated_at = ?
                WHERE kind = 'detail' AND item_type = ? AND item_id = ?
                """,
                (now, item.type, str(item.id)),
            )

    def record_failure(
        self,
        *,
        kind: str,
        item_type: ItemType,
        error_type: str,
        error_message: str,
        item_id: str = "",
        offset: int | None = None,
        url: str = "",
    ) -> None:
        now = _utc_now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO failures (
                    kind, item_type, item_id, offset, url,
                    error_type, error_message, resolved, attempts, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?)
                ON CONFLICT(kind, item_type, item_id, offset) DO UPDATE SET
                    url = CASE WHEN excluded.url != '' THEN excluded.url ELSE failures.url END,
                    error_type = excluded.error_type,
                    error_message = excluded.error_message,
                    resolved = 0,
                    attempts = failures.attempts + 1,
                    updated_at = excluded.updated_at
                """,
                (kind, item_type, str(item_id), offset, url, error_type, error_message, now),
            )

    def get_items_needing_detail(self, item_type: ItemType) -> list[ArchiveItem]:
        rows = self._conn.execute(
            """
            SELECT * FROM items
            WHERE type = ? AND (needs_detail = 1 OR is_full_content = 0)
            ORDER BY created_at DESC, id DESC
            """,
            (item_type,),
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def get_all_items(self, item_type: ItemType | None = None) -> list[ArchiveItem]:
        if item_type:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE type = ? ORDER BY created_at ASC, id ASC",
                (item_type,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM items ORDER BY type ASC, created_at ASC, id ASC"
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def get_existing_ids(self, item_type: ItemType) -> set[str]:
        rows = self._conn.execute(
            "SELECT id FROM items WHERE type = ?", (item_type,)
        ).fetchall()
        return {str(r["id"]) for r in rows}

    def get_unresolved_failures(self, item_type: ItemType | None = None) -> list[dict[str, Any]]:
        if item_type:
            rows = self._conn.execute(
                "SELECT * FROM failures WHERE resolved = 0 AND item_type = ? ORDER BY updated_at DESC",
                (item_type,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM failures WHERE resolved = 0 ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> ArchiveItem:
        quote_raw = json.loads(row["quote_json"]) if row["quote_json"] else None
        return ArchiveItem(
            type=row["type"],
            id=str(row["id"]),
            url=str(row["url"]),
            author_id=str(row["author_id"]),
            author_url_token=str(row["author_url_token"]),
            author_name=str(row["author_name"]),
            title=str(row["title"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            content_markdown=str(row["content_markdown"]),
            excerpt=str(row["excerpt"]),
            images=json.loads(row["images_json"] or "[]"),
            quote=QuoteContent.from_dict(quote_raw),
            question_id=str(row["question_id"] or ""),
            question_title=str(row["question_title"] or ""),
            question_url=str(row["question_url"] or ""),
            voteup_count=row["voteup_count"],
            comment_count=row["comment_count"],
            content_blocks_count=int(row["content_blocks_count"]),
            is_full_content=bool(row["is_full_content"]),
            needs_detail=bool(row["needs_detail"]),
            detail_fetched=bool(row["detail_fetched"]),
            raw_json=json.loads(row["raw_json"] or "{}"),
        )
