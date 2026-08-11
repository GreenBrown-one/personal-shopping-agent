"""Unit tests for the controlled Playwright lifecycle without live network access."""

import asyncio
import stat
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import Error as PlaywrightError

from personal_shopping_agent.browser import (
    BrowserManager,
    BrowserManagerError,
    BrowserManagerSettings,
    BrowserNotStartedError,
    NavigationPolicy,
    NavigationPolicyError,
)
from personal_shopping_agent.browser.manager import PlaywrightFactory
from personal_shopping_agent.local_security import LocalFileSecurityError

CAPTURED_AT = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(awaitable)


async def public_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
    return ("8.8.8.8",)


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeRoute:
    def __init__(self) -> None:
        self.aborted_with: str | None = None
        self.continued = False

    async def abort(self, code: str) -> None:
        self.aborted_with = code

    async def continue_(self) -> None:
        self.continued = True


class FakePage:
    def __init__(self) -> None:
        self.url = "https://shop.example/products"
        self.html = "<html><body>safe fixture</body></html>"
        self.page_title = "Fixture title"
        self.response: FakeResponse | None = FakeResponse(200)
        self.goto_error: PlaywrightError | None = None
        self.screenshot_error: PlaywrightError | None = None
        self.goto_hook: Callable[[], Awaitable[None]] | None = None
        self.goto_calls: list[tuple[str, str, int]] = []
        self.screenshot_calls: list[tuple[str, bool]] = []

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> FakeResponse | None:
        self.goto_calls.append((url, wait_until, timeout))
        if self.goto_hook is not None:
            await self.goto_hook()
        if self.goto_error is not None:
            raise self.goto_error
        return self.response

    async def content(self) -> str:
        return self.html

    async def title(self) -> str:
        return self.page_title

    async def screenshot(self, *, path: str, full_page: bool) -> None:
        self.screenshot_calls.append((path, full_page))
        Path(path).write_bytes(b"fake png")
        if self.screenshot_error is not None:
            raise self.screenshot_error


class FakeContext:
    def __init__(self, page: FakePage, *, has_existing_page: bool = True) -> None:
        self._page = page
        self.pages = [page] if has_existing_page else []
        self.default_timeout: int | None = None
        self.navigation_timeout: int | None = None
        self.route_pattern: str | None = None
        self.route_handler: Any = None
        self.new_page_called = False
        self.closed = False

    def set_default_timeout(self, timeout: int) -> None:
        self.default_timeout = timeout

    def set_default_navigation_timeout(self, timeout: int) -> None:
        self.navigation_timeout = timeout

    async def route(self, pattern: str, handler: Any) -> None:
        self.route_pattern = pattern
        self.route_handler = handler

    async def new_page(self) -> FakePage:
        self.new_page_called = True
        return self._page

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, context: FakeContext) -> None:
        self._context = context
        self.launch_error: PlaywrightError | None = None
        self.launch_options: dict[str, object] = {}

    async def launch_persistent_context(self, **options: object) -> FakeContext:
        self.launch_options = options
        if self.launch_error is not None:
            raise self.launch_error
        return self._context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self._playwright = playwright

    async def start(self) -> FakePlaywright:
        return self._playwright


def make_manager(
    tmp_path: Path,
    *,
    has_existing_page: bool = True,
    maximum_html_bytes: int = 2_000_000,
) -> tuple[BrowserManager, FakePage, FakeContext, FakeChromium, FakePlaywright]:
    page = FakePage()
    context = FakeContext(page, has_existing_page=has_existing_page)
    chromium = FakeChromium(context)
    playwright = FakePlaywright(chromium)
    starter = FakeStarter(playwright)
    factory = cast(PlaywrightFactory, lambda: starter)
    settings = BrowserManagerSettings(
        profile_directory=tmp_path / "profile",
        screenshot_directory=tmp_path / "screenshots",
        headless=False,
        navigation_timeout_ms=1_234,
        maximum_html_bytes=maximum_html_bytes,
    )
    policy = NavigationPolicy({"shop.example"}, resolver=public_resolver)
    manager = BrowserManager(
        policy,
        settings=settings,
        playwright_factory=factory,
        clock=lambda: CAPTURED_AT,
    )
    return manager, page, context, chromium, playwright


@pytest.mark.parametrize(
    "settings",
    [
        BrowserManagerSettings(navigation_timeout_ms=1),
        BrowserManagerSettings(maximum_html_bytes=1),
    ],
)
def test_settings_accept_positive_limits(settings: BrowserManagerSettings) -> None:
    assert settings.navigation_timeout_ms > 0
    assert settings.maximum_html_bytes > 0


def test_settings_reject_nonpositive_limits() -> None:
    with pytest.raises(ValueError, match="navigation_timeout_ms"):
        BrowserManagerSettings(navigation_timeout_ms=0)
    with pytest.raises(ValueError, match="maximum_html_bytes"):
        BrowserManagerSettings(maximum_html_bytes=0)


def test_open_requires_started_manager() -> None:
    manager = BrowserManager(NavigationPolicy({"shop.example"}, resolver=public_resolver))
    with pytest.raises(BrowserNotStartedError):
        run(manager.open("https://shop.example/products"))


def test_context_manager_starts_and_closes_existing_page(tmp_path: Path) -> None:
    manager, _page, context, chromium, playwright = make_manager(tmp_path)

    async def scenario() -> None:
        async with manager as entered:
            assert entered is manager
            with pytest.raises(BrowserManagerError, match="already started"):
                await manager.start()

    run(scenario())

    assert context.default_timeout == 1_234
    assert context.navigation_timeout == 1_234
    assert context.route_pattern == "**/*"
    assert chromium.launch_options == {
        "user_data_dir": str(tmp_path / "profile"),
        "headless": False,
        "accept_downloads": False,
    }
    assert stat.S_IMODE((tmp_path / "profile").stat().st_mode) == 0o700
    assert context.closed
    assert playwright.stopped
    run(manager.close())


def test_start_creates_page_when_profile_has_none(tmp_path: Path) -> None:
    manager, _page, context, _chromium, _playwright = make_manager(
        tmp_path, has_existing_page=False
    )

    run(manager.start())
    run(manager.close())

    assert context.new_page_called


def test_start_sanitizes_playwright_launch_failure(tmp_path: Path) -> None:
    manager, _page, _context, chromium, playwright = make_manager(tmp_path)
    chromium.launch_error = PlaywrightError("raw launch detail")

    with pytest.raises(BrowserManagerError) as captured:
        run(manager.start())

    assert "raw launch detail" not in str(captured.value)
    assert playwright.stopped


def test_open_returns_bounded_snapshot_and_optional_screenshot(tmp_path: Path) -> None:
    manager, page, _context, _chromium, _playwright = make_manager(tmp_path)
    page.page_title = "T" * 600

    async def scenario() -> None:
        await manager.start()
        snapshot = await manager.open("https://shop.example/products", screenshot=True)
        await manager.close()

        assert str(snapshot.requested_url) == "https://shop.example/products"
        assert str(snapshot.final_url) == "https://shop.example/products"
        assert snapshot.status_code == 200
        assert len(snapshot.title) == 500
        assert snapshot.html == page.html
        assert snapshot.captured_at == CAPTURED_AT
        assert snapshot.screenshot_path is not None

    run(scenario())

    assert page.goto_calls == [("https://shop.example/products", "domcontentloaded", 1_234)]
    assert len(page.screenshot_calls) == 1
    screenshot_path, full_page = page.screenshot_calls[0]
    assert screenshot_path.startswith(str(tmp_path / "screenshots"))
    assert full_page
    assert stat.S_IMODE((tmp_path / "screenshots").stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(screenshot_path).stat().st_mode) == 0o600


def test_start_rejects_an_insecure_existing_profile(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o755)
    profile.chmod(0o755)
    manager, _page, context, _chromium, playwright = make_manager(tmp_path)

    with pytest.raises(BrowserManagerError, match="profile directory is not private"):
        run(manager.start())

    assert context.route_pattern is None
    assert playwright.stopped is False


def test_screenshot_failures_are_sanitized_and_remove_partial_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, page, _context, _chromium, _playwright = make_manager(tmp_path)
    page.screenshot_error = PlaywrightError("raw screenshot detail")

    async def playwright_failure() -> None:
        await manager.start()
        with pytest.raises(BrowserManagerError) as captured:
            await manager.open("https://shop.example/products", screenshot=True)
        assert "raw screenshot detail" not in str(captured.value)
        await manager.close()

    run(playwright_failure())
    assert not tuple((tmp_path / "screenshots").glob("*.png"))

    second, _page, _context, _chromium, _playwright = make_manager(tmp_path / "second")

    def fail_security(_: Path) -> None:
        raise LocalFileSecurityError("raw local path detail")

    monkeypatch.setattr(
        "personal_shopping_agent.browser.manager.secure_existing_private_file",
        fail_security,
    )

    async def private_storage_failure() -> None:
        await second.start()
        with pytest.raises(BrowserManagerError) as captured:
            await second.open("https://shop.example/products", screenshot=True)
        assert "raw local path detail" not in str(captured.value)
        await second.close()

    run(private_storage_failure())
    assert not tuple((tmp_path / "second" / "screenshots").glob("*.png"))


def test_screenshot_rejects_an_insecure_existing_directory(tmp_path: Path) -> None:
    manager, _page, _context, _chromium, _playwright = make_manager(tmp_path)
    screenshots = tmp_path / "screenshots"
    screenshots.mkdir(mode=0o755)
    screenshots.chmod(0o755)

    async def scenario() -> None:
        await manager.start()
        with pytest.raises(BrowserManagerError, match="screenshot directory is not private"):
            await manager.open("https://shop.example/products", screenshot=True)
        await manager.close()

    run(scenario())


def test_open_allows_missing_navigation_response_without_screenshot(tmp_path: Path) -> None:
    manager, page, _context, _chromium, _playwright = make_manager(tmp_path)
    page.response = None

    async def scenario() -> None:
        await manager.start()
        snapshot = await manager.open("https://shop.example/products")
        await manager.close()
        assert snapshot.status_code == 0
        assert snapshot.screenshot_path is None

    run(scenario())
    assert page.screenshot_calls == []


def test_open_does_not_capture_unsuccessful_response_screenshot(tmp_path: Path) -> None:
    manager, page, _context, _chromium, _playwright = make_manager(tmp_path)
    page.response = FakeResponse(403)

    async def scenario() -> None:
        await manager.start()
        snapshot = await manager.open("https://shop.example/products", screenshot=True)
        await manager.close()
        assert snapshot.status_code == 403
        assert snapshot.screenshot_path is None

    run(scenario())
    assert page.screenshot_calls == []


def test_open_rejects_oversized_html(tmp_path: Path) -> None:
    manager, page, _context, _chromium, _playwright = make_manager(tmp_path, maximum_html_bytes=3)
    page.html = "four"

    async def scenario() -> None:
        await manager.start()
        with pytest.raises(BrowserManagerError, match="size limit"):
            await manager.open("https://shop.example/products")
        await manager.close()

    run(scenario())


def test_open_sanitizes_navigation_failure(tmp_path: Path) -> None:
    manager, page, _context, _chromium, _playwright = make_manager(tmp_path)
    page.goto_error = PlaywrightError("raw page detail")

    async def scenario() -> None:
        await manager.start()
        with pytest.raises(BrowserManagerError) as captured:
            await manager.open("https://shop.example/products")
        assert "raw page detail" not in str(captured.value)
        await manager.close()

    run(scenario())


def test_route_guard_allows_public_requests_and_blocks_unsafe_redirects(tmp_path: Path) -> None:
    manager, page, context, _chromium, _playwright = make_manager(tmp_path)
    allowed_route = FakeRoute()
    blocked_route = FakeRoute()

    async def block_during_navigation() -> None:
        route_handler = cast(Callable[[Any, Any], Awaitable[None]], context.route_handler)
        await route_handler(
            cast(Any, blocked_route), cast(Any, FakeRequest("http://shop.example/tracker"))
        )

    page.goto_hook = block_during_navigation
    page.goto_error = PlaywrightError("route aborted")

    async def scenario() -> None:
        await manager.start()
        route_handler = cast(Callable[[Any, Any], Awaitable[None]], context.route_handler)
        await route_handler(
            cast(Any, allowed_route),
            cast(Any, FakeRequest("https://shop.example/image.png")),
        )
        with pytest.raises(NavigationPolicyError) as captured:
            await manager.open("https://shop.example/products")
        assert captured.value.code == "scheme_not_allowed"
        await manager.close()

    run(scenario())

    assert allowed_route.continued
    assert allowed_route.aborted_with is None
    assert blocked_route.aborted_with == "blockedbyclient"


def test_open_revalidates_final_redirect_url(tmp_path: Path) -> None:
    manager, page, _context, _chromium, _playwright = make_manager(tmp_path)
    page.url = "https://other.example/products"

    async def scenario() -> None:
        await manager.start()
        with pytest.raises(NavigationPolicyError) as captured:
            await manager.open("https://shop.example/products")
        assert captured.value.code == "host_not_allowed"
        await manager.close()

    run(scenario())
