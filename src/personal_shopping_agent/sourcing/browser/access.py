"""Bounded request pacing, retry, and in-flight deduplication for page collection."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from personal_shopping_agent.application.discovery import CollectedPage
from personal_shopping_agent.browser.manager import (
    BrowserManagerError,
    TransientBrowserManagerError,
)

MonotonicClock = Callable[[], float]
AsyncSleeper = Callable[[float], Awaitable[None]]
_ALLOWED_RETRYABLE_STATUSES = frozenset({0, 408, 425, 500, 502, 503, 504})
_MANDATORY_RESTRICTED_STATUSES = frozenset({401, 403, 429})


class StatusPage(CollectedPage, Protocol):
    """Collected page with the HTTP status needed for retry decisions."""

    @property
    def status_code(self) -> int: ...


class StatusPageCollector(Protocol):
    """Collector port implemented structurally by the controlled BrowserManager."""

    async def open(self, url: str, *, screenshot: bool = False) -> StatusPage: ...


class PageCollectionFailedError(BrowserManagerError):
    """Sanitized terminal response or exhausted transient collection failure."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class PageAccessRestrictedError(PageCollectionFailedError):
    """HTTP access control or throttling signal that must never be retried."""


@dataclass(frozen=True, slots=True)
class PageAccessSettings:
    """Conservative per-host pacing and bounded transient retry settings."""

    minimum_interval_seconds: float = 3.0
    maximum_attempts: int = 2
    retry_base_delay_seconds: float = 1.0
    retry_maximum_delay_seconds: float = 4.0
    retryable_status_codes: frozenset[int] = field(
        default_factory=lambda: _ALLOWED_RETRYABLE_STATUSES
    )
    restricted_status_codes: frozenset[int] = field(
        default_factory=lambda: _MANDATORY_RESTRICTED_STATUSES
    )

    def __post_init__(self) -> None:
        if self.minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds cannot be negative")
        if not 1 <= self.maximum_attempts <= 3:
            raise ValueError("maximum_attempts must be between 1 and 3")
        if self.retry_base_delay_seconds < 0:
            raise ValueError("retry_base_delay_seconds cannot be negative")
        if self.retry_maximum_delay_seconds < self.retry_base_delay_seconds:
            raise ValueError(
                "retry_maximum_delay_seconds cannot be lower than retry_base_delay_seconds"
            )
        invalid_statuses = (self.retryable_status_codes | self.restricted_status_codes) - set(
            range(600)
        )
        if invalid_statuses:
            raise ValueError("HTTP status policies must contain values from 0 to 599")
        if self.retryable_status_codes - _ALLOWED_RETRYABLE_STATUSES:
            raise ValueError("retryable HTTP statuses contain an unsupported value")
        if not self.restricted_status_codes >= _MANDATORY_RESTRICTED_STATUSES:
            raise ValueError("restricted HTTP statuses must include 401, 403, and 429")
        if self.retryable_status_codes & self.restricted_status_codes:
            raise ValueError("retryable and restricted HTTP statuses must not overlap")


class ControlledPageCollector:
    """Decorate a page collector with low-frequency and fail-closed access rules."""

    def __init__(
        self,
        collector: StatusPageCollector,
        *,
        settings: PageAccessSettings | None = None,
        clock: MonotonicClock = time.monotonic,
        sleeper: AsyncSleeper = asyncio.sleep,
    ) -> None:
        self._collector = collector
        self._settings = settings or PageAccessSettings()
        self._clock = clock
        self._sleeper = sleeper
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_started_at: dict[str, float] = {}
        self._inflight: dict[str, asyncio.Task[StatusPage]] = {}

    async def open(self, url: str, *, screenshot: bool = False) -> StatusPage:
        """Collect safely, sharing only simultaneous non-screenshot requests."""

        hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
        if not hostname:
            raise PageCollectionFailedError(
                "invalid_url", "The destination URL has no valid hostname."
            )
        if screenshot:
            return await self._open_bounded(url, hostname, screenshot=True)

        existing = self._inflight.get(url)
        if existing is not None:
            return await asyncio.shield(existing)
        task = asyncio.create_task(self._open_bounded(url, hostname, screenshot=False))
        self._inflight[url] = task
        task.add_done_callback(lambda completed: self._drop_inflight(url, completed))
        return await asyncio.shield(task)

    def _drop_inflight(self, url: str, task: asyncio.Task[StatusPage]) -> None:
        del task
        self._inflight.pop(url, None)

    async def _open_bounded(
        self,
        url: str,
        hostname: str,
        *,
        screenshot: bool,
    ) -> StatusPage:
        lock = self._host_locks.setdefault(hostname, asyncio.Lock())
        async with lock:
            maximum_attempts = 1 if screenshot else self._settings.maximum_attempts
            for attempt in range(1, maximum_attempts + 1):
                await self._wait_for_host_slot(hostname)
                try:
                    page = await self._collector.open(url, screenshot=screenshot)
                except TransientBrowserManagerError as error:
                    if attempt == maximum_attempts:
                        raise PageCollectionFailedError(
                            "retry_exhausted",
                            "The destination page remained unavailable after bounded retries.",
                        ) from error
                    await self._wait_before_retry(attempt)
                    continue

                if page.status_code in self._settings.restricted_status_codes:
                    raise PageAccessRestrictedError(
                        "http_access_restricted",
                        "The destination refused or throttled automated page access.",
                        status_code=page.status_code,
                    )
                if page.status_code in self._settings.retryable_status_codes:
                    if attempt == maximum_attempts:
                        raise PageCollectionFailedError(
                            "retry_exhausted",
                            "The destination returned only transient failure responses.",
                            status_code=page.status_code,
                        )
                    await self._wait_before_retry(attempt)
                    continue
                if page.status_code >= 400:
                    raise PageCollectionFailedError(
                        "http_error",
                        "The destination returned an unsuccessful response.",
                        status_code=page.status_code,
                    )
                return page

        raise AssertionError(  # pragma: no cover - maximum_attempts is validated as positive
            "bounded page collection exhausted without a result"
        )

    async def _wait_for_host_slot(self, hostname: str) -> None:
        now = self._clock()
        previous = self._last_started_at.get(hostname)
        if previous is not None:
            remaining = self._settings.minimum_interval_seconds - (now - previous)
            if remaining > 0:
                await self._sleeper(remaining)
        self._last_started_at[hostname] = self._clock()

    async def _wait_before_retry(self, attempt: int) -> None:
        delay = min(
            self._settings.retry_base_delay_seconds * (2 ** (attempt - 1)),
            self._settings.retry_maximum_delay_seconds,
        )
        if delay > 0:
            await self._sleeper(delay)
