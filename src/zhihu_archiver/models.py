from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Literal
from urllib.parse import urlparse

ItemType = Literal["article", "pin", "answer", "question"]

ITEM_TYPE_ALIASES: dict[str, ItemType] = {
    "article": "article",
    "articles": "article",
    "post": "article",
    "posts": "article",
    "pin": "pin",
    "pins": "pin",
    "answer": "answer",
    "answers": "answer",
    "question": "question",
    "questions": "question",
    "ask": "question",
    "asks": "question",
}


def parse_item_types(spec: str) -> tuple[ItemType, ...]:
    """Parse comma-separated item types like 'articles,pins,answers,questions'."""
    result: list[ItemType] = []
    for raw in spec.split(","):
        key = raw.strip().lower()
        if not key:
            continue
        if key not in ITEM_TYPE_ALIASES:
            valid = ", ".join(sorted({"articles", "pins", "answers", "questions"}))
            raise ValueError(f"Unknown item type {raw!r}. Valid choices: {valid}")
        canonical = ITEM_TYPE_ALIASES[key]
        if canonical not in result:
            result.append(canonical)
    if not result:
        raise ValueError("At least one item type must be specified.")
    return tuple(result)


def compute_was_edited(created_at: str | None, updated_at: str | None, threshold_seconds: int = 3600) -> bool:
    """Return True if updated_at is later than created_at by more than threshold_seconds."""
    if not created_at or not updated_at:
        return False
    try:
        c_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        u_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        return (u_dt - c_dt).total_seconds() > threshold_seconds
    except ValueError:
        return created_at != updated_at


def parse_user_url_token(url_or_token: str) -> str:
    """Extract Zhihu user url_token from a profile URL or return a bare token."""
    raw = url_or_token.strip()
    if not raw:
        raise ValueError("User URL or url_token cannot be empty.")

    if "://" in raw or raw.startswith("www.zhihu.com") or raw.startswith("zhihu.com"):
        if "://" not in raw:
            raw = "https://" + raw
        parsed = urlparse(raw)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"people", "org"}:
            return parts[1]
        raise ValueError(
            f"Could not extract Zhihu user url_token from URL: {url_or_token!r}. "
            "Expected format like https://www.zhihu.com/people/<url_token>"
        )

    if not re.fullmatch(r"[A-Za-z0-9._-]+", raw):
        raise ValueError(f"Invalid Zhihu url_token: {raw!r}")
    return raw


def timestamp_to_iso(value: Any) -> str | None:
    """Convert Unix timestamp (seconds or milliseconds) or ISO string to UTC ISO-8601."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts <= 0:
            return None
        if ts > 1e12:  # milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return timestamp_to_iso(int(text))
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            return text
    return None


@dataclass(slots=True)
class QuoteContent:
    """Quoted or reposted content inside a pin, kept distinct from author's commentary."""

    quote_type: str = "pin"
    id: str = ""
    title: str = ""
    url: str = ""
    author_id: str = ""
    author_url_token: str = ""
    author_name: str = ""
    created_at: str | None = None
    content_markdown: str = ""
    images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote_type": self.quote_type,
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "author_id": self.author_id,
            "author_url_token": self.author_url_token,
            "author_name": self.author_name,
            "created_at": self.created_at,
            "content_markdown": self.content_markdown,
            "images": list(self.images),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> QuoteContent | None:
        if not data:
            return None
        return cls(
            quote_type=str(data.get("quote_type") or "pin"),
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            url=str(data.get("url") or ""),
            author_id=str(data.get("author_id") or ""),
            author_url_token=str(data.get("author_url_token") or ""),
            author_name=str(data.get("author_name") or ""),
            created_at=data.get("created_at"),
            content_markdown=str(data.get("content_markdown") or ""),
            images=list(data.get("images") or []),
        )


@dataclass(slots=True)
class ArchiveItem:
    """Normalized article, pin, answer, or question item."""

    type: ItemType
    id: str
    url: str
    author_url_token: str
    author_id: str = ""
    author_name: str = ""
    title: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    content_markdown: str = ""
    excerpt: str = ""
    images: list[str] = field(default_factory=list)
    quote: QuoteContent | None = None
    question_id: str = ""
    question_title: str = ""
    question_url: str = ""
    voteup_count: int | None = None
    comment_count: int | None = None
    content_blocks_count: int = 0
    is_full_content: bool = False
    needs_detail: bool = False
    detail_fetched: bool = False
    raw_json: dict[str, Any] = field(default_factory=dict)

    @property
    def was_edited(self) -> bool:
        return compute_was_edited(self.created_at, self.updated_at)

    def to_corpus_dict(self) -> dict[str, Any]:
        """Serialize to corpus.jsonl record format."""
        record: dict[str, Any] = {
            "type": self.type,
            "id": str(self.id),
            "title": self.title or None,
            "url": self.url,
            "author": {
                "id": self.author_id,
                "url_token": self.author_url_token,
                "name": self.author_name,
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "was_edited": self.was_edited,
            "voteup_count": self.voteup_count,
            "comment_count": self.comment_count,
            "content_markdown": self.content_markdown,
            "images": list(self.images),
            "is_full_content": self.is_full_content,
            "detail_fetched": self.detail_fetched,
        }
        if self.question_id or self.question_title or self.question_url:
            record["question"] = {
                "id": self.question_id,
                "title": self.question_title,
                "url": self.question_url,
            }
        if self.quote is not None:
            record["quote"] = self.quote.to_dict()
        return record


@dataclass(slots=True)
class PageResult:
    """Normalized page returned by a list endpoint."""

    items: list[ArchiveItem]
    is_end: bool
    next_offset: int | None = None
    next_cursor: str | None = None
    api_total: int | None = None
    raw_count: int = 0
    raw_paging: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class UserProfile:
    """Basic author metadata used for verification and reporting."""

    url_token: str
    member_id: str = ""
    name: str = ""
    articles_count: int | None = None
    pins_count: int | None = None
    answers_count: int | None = None
    questions_count: int | None = None
    follower_count: int | None = None
