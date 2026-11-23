from __future__ import annotations

from abc import ABC, abstractmethod

from zhihu_archiver.models import ArchiveItem, PageResult, UserProfile


class ProviderError(Exception):
    """Base error for Zhihu data providers."""


class FatalProviderError(ProviderError):
    """Non-retryable provider error that should halt the current task."""


class AuthError(FatalProviderError):
    """Authentication or authorization failure (401/403)."""


class QuotaError(FatalProviderError):
    """Insufficient quota or balance (402 or provider quota message)."""


class DuplicateCursorError(FatalProviderError):
    """Pagination returned a duplicate page or failed to advance."""


class RetryableProviderError(ProviderError):
    """Transient error that may succeed on retry."""


class RateLimitError(RetryableProviderError):
    """HTTP 429 or upstream rate limit."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientError(RetryableProviderError):
    """Timeout, network error, or 5xx upstream failure."""


class ZhihuProvider(ABC):
    """Abstract interface for fetching Zhihu user articles and pins."""

    name: str = "base"

    def fetch_user_info(self, user_url_token: str) -> UserProfile | None:
        return None

    @abstractmethod
    def fetch_user_articles(
        self,
        user_url_token: str,
        offset: int = 0,
        limit: int = 20,
        cursor: str | None = None,
    ) -> PageResult:
        raise NotImplementedError

    @abstractmethod
    def fetch_user_pins(
        self,
        user_url_token: str,
        offset: int = 0,
        limit: int = 20,
        cursor: str | None = None,
    ) -> PageResult:
        raise NotImplementedError

    @abstractmethod
    def fetch_article_detail(self, article_id: str) -> ArchiveItem:
        raise NotImplementedError

    @abstractmethod
    def fetch_pin_detail(self, pin_id: str) -> ArchiveItem:
        raise NotImplementedError

    def fetch_user_answers(
        self,
        user_url_token: str,
        offset: int = 0,
        limit: int = 20,
        sort_by: str = "created",
        cursor: str | None = None,
    ) -> PageResult:
        raise ProviderError(f"Provider {self.name!r} does not implement fetch_user_answers.")

    def fetch_user_questions(
        self,
        user_url_token: str,
        offset: int = 0,
        limit: int = 20,
        cursor: str | None = None,
    ) -> PageResult:
        raise ProviderError(f"Provider {self.name!r} does not implement fetch_user_questions.")

    def fetch_answer_detail(self, answer_id: str) -> ArchiveItem:
        raise ProviderError(f"Provider {self.name!r} does not implement fetch_answer_detail.")

    def close(self) -> None:
        return None
