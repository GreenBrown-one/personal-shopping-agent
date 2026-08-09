"""Unit tests for low-frequency, bounded, and non-persistent page collection."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import HttpUrl

from personal_shopping_agent.browser import (
    BrowserManagerError,
    ControlledPageCollector,
    PageAccessRestrictedError,
    PageAccessSettings,
    PageCollectionFailedError,
    StatusPage,
    TransientBrowserManagerError,
)

CAPTURED_AT = datetime(2026, 8, 9, 20, 0, tzinfo=UTC)


@dataclass(frozen=True)
class FakeCollectedPage:
    final_url: HttpUrl
    html: str = "<html>fixture</html>"
    captured_at: datetime = CAPTURED_AT
    status_code: int = 200


class FakeTime:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.value += delay

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeCollector:
    def __init__(
        self,
        outcomes: list[FakeCollectedPage | BrowserManagerError],
        clock: FakeTime,
    ) -> None:
        self.outcomes = outcomes
        self.clock = clock
        self.calls: list[tuple[str, bool, float]] = []

    async def open(self, url: str, *, screenshot: bool = False) -> FakeCollectedPage:
        self.calls.append((url, screenshot, self.clock()))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BrowserManagerError):
            raise outcome
        return outcome


def page(url: str = "https://shop.example/item", *, status: int = 200) -> FakeCollectedPage:
    return FakeCollectedPage(final_url=HttpUrl(url), status_code=status)


def test_access_settings_accept_safe_defaults_and_reject_invalid_limits() -> None:
    settings = PageAccessSettings()
    assert settings.minimum_interval_seconds == 3.0
    assert settings.maximum_attempts == 2

    invalid = (
        ({"minimum_interval_seconds": -1}, "minimum_interval_seconds"),
        ({"maximum_attempts": 0}, "maximum_attempts"),
        ({"maximum_attempts": 4}, "maximum_attempts"),
        ({"retry_base_delay_seconds": -1}, "retry_base_delay_seconds"),
        (
            {"retry_base_delay_seconds": 2, "retry_maximum_delay_seconds": 1},
            "retry_maximum_delay_seconds",
        ),
        ({"retryable_status_codes": frozenset({600})}, "HTTP status"),
        ({"retryable_status_codes": frozenset({404})}, "unsupported"),
        ({"restricted_status_codes": frozenset[int]()}, "must include"),
        (
            {
                "retryable_status_codes": frozenset({408}),
                "restricted_status_codes": frozenset({401, 403, 408, 429}),
            },
            "must not overlap",
        ),
    )
    for values, message in invalid:
        with pytest.raises(ValueError, match=message):
            PageAccessSettings(**values)  # type: ignore[arg-type]


def test_collector_rate_limits_same_host_but_not_a_new_host() -> None:
    clock = FakeTime()
    underlying = FakeCollector(
        [page(), page("https://shop.example/other"), page("https://other.example/item")],
        clock,
    )
    collector = ControlledPageCollector(
        underlying,
        settings=PageAccessSettings(maximum_attempts=1, minimum_interval_seconds=5),
        clock=clock,
        sleeper=clock.sleep,
    )

    async def scenario() -> None:
        await collector.open("https://shop.example/item")
        clock.advance(1)
        await collector.open("https://shop.example/other")
        clock.advance(10)
        await collector.open("https://other.example/item")

    asyncio.run(scenario())

    assert clock.sleeps == [4.0]
    assert [call[2] for call in underlying.calls] == [0.0, 5.0, 15.0]


def test_collector_retries_transient_navigation_with_backoff_and_host_spacing() -> None:
    clock = FakeTime()
    underlying = FakeCollector(
        [TransientBrowserManagerError("sanitized"), page()],
        clock,
    )
    collector = ControlledPageCollector(
        underlying,
        settings=PageAccessSettings(
            minimum_interval_seconds=2,
            maximum_attempts=2,
            retry_base_delay_seconds=1,
        ),
        clock=clock,
        sleeper=clock.sleep,
    )

    result = asyncio.run(collector.open("https://shop.example/item"))

    assert result.status_code == 200
    assert clock.sleeps == [1, 1.0]
    assert [call[2] for call in underlying.calls] == [0.0, 2.0]


def test_collector_caps_backoff_and_reports_exhausted_navigation() -> None:
    clock = FakeTime()
    underlying = FakeCollector(
        [
            TransientBrowserManagerError("first"),
            TransientBrowserManagerError("second"),
            TransientBrowserManagerError("third"),
        ],
        clock,
    )
    collector = ControlledPageCollector(
        underlying,
        settings=PageAccessSettings(
            minimum_interval_seconds=0,
            maximum_attempts=3,
            retry_base_delay_seconds=3,
            retry_maximum_delay_seconds=4,
        ),
        clock=clock,
        sleeper=clock.sleep,
    )

    with pytest.raises(PageCollectionFailedError) as captured:
        asyncio.run(collector.open("https://shop.example/item"))

    assert captured.value.code == "retry_exhausted"
    assert captured.value.status_code is None
    assert clock.sleeps == [3, 4]


def test_collector_retries_only_declared_transient_http_statuses() -> None:
    clock = FakeTime()
    underlying = FakeCollector([page(status=503), page()], clock)
    collector = ControlledPageCollector(
        underlying,
        settings=PageAccessSettings(
            minimum_interval_seconds=0,
            maximum_attempts=2,
            retry_base_delay_seconds=0,
            retry_maximum_delay_seconds=0,
        ),
        clock=clock,
        sleeper=clock.sleep,
    )

    result = asyncio.run(collector.open("https://shop.example/item"))

    assert result.status_code == 200
    assert len(underlying.calls) == 2
    assert clock.sleeps == []


@pytest.mark.parametrize("status", [401, 403, 429])
def test_collector_never_retries_access_restrictions(status: int) -> None:
    clock = FakeTime()
    underlying = FakeCollector([page(status=status), page()], clock)
    collector = ControlledPageCollector(underlying, clock=clock, sleeper=clock.sleep)

    with pytest.raises(PageAccessRestrictedError) as captured:
        asyncio.run(collector.open("https://shop.example/item"))

    assert captured.value.code == "http_access_restricted"
    assert captured.value.status_code == status
    assert len(underlying.calls) == 1


def test_collector_does_not_retry_terminal_http_or_browser_errors() -> None:
    clock = FakeTime()
    http_collector = FakeCollector([page(status=404), page()], clock)
    collector = ControlledPageCollector(http_collector, clock=clock, sleeper=clock.sleep)
    with pytest.raises(PageCollectionFailedError) as captured:
        asyncio.run(collector.open("https://shop.example/missing"))
    assert captured.value.code == "http_error"
    assert captured.value.status_code == 404
    assert len(http_collector.calls) == 1

    browser_collector = FakeCollector([BrowserManagerError("safe size failure"), page()], clock)
    collector = ControlledPageCollector(browser_collector, clock=clock, sleeper=clock.sleep)
    with pytest.raises(BrowserManagerError, match="safe size failure"):
        asyncio.run(collector.open("https://shop.example/large"))
    assert len(browser_collector.calls) == 1


def test_collector_reports_exhausted_transient_response_and_invalid_url() -> None:
    clock = FakeTime()
    underlying = FakeCollector([page(status=0)], clock)
    collector = ControlledPageCollector(
        underlying,
        settings=PageAccessSettings(maximum_attempts=1),
        clock=clock,
        sleeper=clock.sleep,
    )

    with pytest.raises(PageCollectionFailedError) as captured:
        asyncio.run(collector.open("https://shop.example/item"))
    assert captured.value.code == "retry_exhausted"
    assert captured.value.status_code == 0

    with pytest.raises(PageCollectionFailedError) as invalid:
        asyncio.run(collector.open("not-a-url"))
    assert invalid.value.code == "invalid_url"


def test_screenshot_request_is_never_retried_or_coalesced() -> None:
    clock = FakeTime()
    underlying = FakeCollector(
        [TransientBrowserManagerError("transient"), page(), page()],
        clock,
    )
    collector = ControlledPageCollector(underlying, clock=clock, sleeper=clock.sleep)

    with pytest.raises(PageCollectionFailedError):
        asyncio.run(collector.open("https://shop.example/item", screenshot=True))
    first = asyncio.run(collector.open("https://shop.example/item", screenshot=True))
    second = asyncio.run(collector.open("https://shop.example/item", screenshot=True))

    assert first is not second
    assert [call[1] for call in underlying.calls] == [True, True, True]


class BlockingCollector:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def open(self, url: str, *, screenshot: bool = False) -> FakeCollectedPage:
        del screenshot
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return page(url)


def test_simultaneous_identical_requests_share_work_but_no_completed_html_cache() -> None:
    async def scenario() -> tuple[StatusPage, StatusPage, StatusPage, int]:
        underlying = BlockingCollector()
        collector = ControlledPageCollector(
            underlying,
            settings=PageAccessSettings(minimum_interval_seconds=0),
        )
        first_task = asyncio.create_task(collector.open("https://shop.example/item"))
        await underlying.started.wait()
        second_task = asyncio.create_task(collector.open("https://shop.example/item"))
        await asyncio.sleep(0)
        underlying.release.set()
        first, second = await asyncio.gather(first_task, second_task)
        third = await collector.open("https://shop.example/item")
        return first, second, third, underlying.calls

    first, second, third, calls = asyncio.run(scenario())

    assert first is second
    assert third is not first
    assert calls == 2
