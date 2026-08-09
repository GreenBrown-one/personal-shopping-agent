"""Playwright lifecycle and snapshot boundary for controlled platform access."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Protocol
from uuid import uuid4

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    Request,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl

from personal_shopping_agent.browser.policy import NavigationPolicy, NavigationPolicyError


class BrowserManagerError(RuntimeError):
    """Sanitized browser failure that does not expose page content or credentials."""


class BrowserNotStartedError(BrowserManagerError):
    """Raised when navigation is attempted outside the managed lifecycle."""


class BrowserSnapshot(BaseModel):
    """Bounded page data returned to a platform parser."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_url: HttpUrl
    final_url: HttpUrl
    status_code: int = Field(ge=0, le=599)
    title: str = Field(max_length=500)
    html: str
    captured_at: AwareDatetime
    screenshot_path: str | None = None


@dataclass(frozen=True, slots=True)
class BrowserManagerSettings:
    """Local-only browser settings; the profile directory must remain untracked."""

    profile_directory: Path = Path("data/browser-profiles/default")
    screenshot_directory: Path = Path("screenshots")
    headless: bool = True
    navigation_timeout_ms: int = 20_000
    maximum_html_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if self.navigation_timeout_ms < 1:
            raise ValueError("navigation_timeout_ms must be positive")
        if self.maximum_html_bytes < 1:
            raise ValueError("maximum_html_bytes must be positive")


class PlaywrightStarter(Protocol):
    """Public subset returned by ``async_playwright`` and test launchers."""

    async def start(self) -> Playwright: ...


PlaywrightFactory = Callable[[], PlaywrightStarter]
Clock = Callable[[], datetime]


class BrowserManager:
    """Own one persistent, user-controlled browser context with guarded navigation."""

    def __init__(
        self,
        policy: NavigationPolicy,
        *,
        settings: BrowserManagerSettings | None = None,
        playwright_factory: PlaywrightFactory = async_playwright,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._policy = policy
        self._settings = settings or BrowserManagerSettings()
        self._playwright_factory = playwright_factory
        self._clock = clock
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._last_violation: NavigationPolicyError | None = None
        self._navigation_lock = asyncio.Lock()

    async def __aenter__(self) -> BrowserManager:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = (exc_type, exc_value, traceback)
        await self.close()

    async def start(self) -> None:
        """Launch Chromium with a dedicated local profile and guarded requests."""

        if self._context is not None:
            raise BrowserManagerError("The browser manager is already started.")

        self._settings.profile_directory.mkdir(parents=True, exist_ok=True)
        manager = self._playwright_factory()
        playwright = await manager.start()
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._settings.profile_directory),
                headless=self._settings.headless,
                accept_downloads=False,
            )
        except PlaywrightError as error:
            await playwright.stop()
            raise BrowserManagerError(
                "Chromium could not start. Install the Playwright Chromium binary first."
            ) from error

        context.set_default_timeout(self._settings.navigation_timeout_ms)
        context.set_default_navigation_timeout(self._settings.navigation_timeout_ms)
        await context.route("**/*", self._guard_route)

        self._playwright = playwright
        self._context = context
        self._page = context.pages[0] if context.pages else await context.new_page()

    async def close(self) -> None:
        """Close the profile context and Playwright process; safe to call repeatedly."""

        context, playwright = self._context, self._playwright
        self._context = None
        self._page = None
        self._playwright = None
        if context is not None:
            await context.close()
        if playwright is not None:
            await playwright.stop()

    async def open(self, url: str, *, screenshot: bool = False) -> BrowserSnapshot:
        """Navigate once and return a bounded inert HTML snapshot for an adapter."""

        page = self._page
        if page is None:
            raise BrowserNotStartedError("Start the browser manager before navigation.")

        await self._policy.validate(url)
        async with self._navigation_lock:
            self._last_violation = None
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._settings.navigation_timeout_ms,
                )
            except PlaywrightError as error:
                if self._last_violation is not None:
                    raise self._last_violation from None
                raise BrowserManagerError("The destination page could not be opened.") from error

            await self._policy.validate(page.url)
            html = await page.content()
            if len(html.encode("utf-8")) > self._settings.maximum_html_bytes:
                raise BrowserManagerError("The destination page exceeded the safe HTML size limit.")

            screenshot_path: str | None = None
            if screenshot:
                self._settings.screenshot_directory.mkdir(parents=True, exist_ok=True)
                path = self._settings.screenshot_directory / f"page-{uuid4().hex}.png"
                await page.screenshot(path=str(path), full_page=True)
                screenshot_path = str(path)

            return BrowserSnapshot.model_validate(
                {
                    "requested_url": url,
                    "final_url": page.url,
                    "status_code": response.status if response is not None else 0,
                    "title": (await page.title())[:500],
                    "html": html,
                    "captured_at": self._clock(),
                    "screenshot_path": screenshot_path,
                }
            )

    async def _guard_route(self, route: Route, request: Request) -> None:
        try:
            await self._policy.validate(request.url)
        except NavigationPolicyError as violation:
            self._last_violation = violation
            await route.abort("blockedbyclient")
        else:
            await route.continue_()
