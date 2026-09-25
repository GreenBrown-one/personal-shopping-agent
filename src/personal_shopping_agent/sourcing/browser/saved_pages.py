"""Pages the user saved from their own browser, served through the PageCollector port.

Nothing here touches the network. Saved files are untrusted data: they are only read and parsed,
never executed, and only regular files in the inbox directory are considered.
"""

import email
import email.policy
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

from personal_shopping_agent.infrastructure.local_security import (
    LocalFileSecurityError,
    require_private_directory,
)
from personal_shopping_agent.sourcing.browser.manager import BrowserSnapshot
from personal_shopping_agent.sourcing.discovery import PlatformAccessRestrictedError

SAVED_PAGE_SUFFIXES = frozenset({".html", ".htm", ".mhtml", ".mht"})
MAXIMUM_SAVED_PAGE_BYTES = 10_000_000
MAXIMUM_SAVED_PAGES = 500
_SAVED_FROM = re.compile(r"<!--\s*saved from url=\(\d+\)(?P<url>https?://\S+?)\s*-->", re.I)
_CANONICAL = re.compile(
    r"<link\b[^>]*\brel=[\"']canonical[\"'][^>]*\bhref=[\"'](?P<url>https?://[^\"']+)[\"']",
    re.I,
)
_TITLE = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.I | re.S)

PageKey = Callable[[str], str | None]


class SavedPageMissingError(PlatformAccessRestrictedError):
    """The pipeline needs a page the user has not saved yet; it names the exact URL."""

    def __init__(self, url: str) -> None:
        super().__init__(
            "page_not_saved",
            f"Open {url} in your own browser, save it as 'Webpage, Complete' (.html) or "
            "'Webpage, Single File' (.mhtml) into data/inbox, then run the pipeline again.",
        )
        self.url = url


class SavedPageInboxError(PlatformAccessRestrictedError):
    """The inbox is not a private directory, so saved pages could be read by other users."""

    def __init__(self) -> None:
        super().__init__(
            "inbox_not_private",
            "data/inbox must be a private directory: remove access for other users "
            "(for example `chmod 700 data/inbox`), then run again.",
        )


@dataclass(frozen=True, slots=True)
class SavedPage:
    """One recognized saved page, keyed by the platform's page identity."""

    key: str
    url: str
    path: Path
    captured_at: datetime
    html: str


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_mhtml(data: bytes) -> tuple[str, str | None, datetime | None]:
    message = email.message_from_bytes(data, policy=email.policy.default)
    assert isinstance(message, EmailMessage)
    location = message.get("Snapshot-Content-Location")
    captured_at: datetime | None = None
    if message.get("Date"):
        try:
            captured_at = parsedate_to_datetime(str(message["Date"])).astimezone(UTC)
        except (TypeError, ValueError):
            captured_at = None
    for part in message.walk():
        if part.get_content_type() == "text/html":
            assert isinstance(part, EmailMessage)
            html = part.get_content()
            return (
                str(html),
                str(location or part.get("Content-Location") or "") or None,
                (captured_at),
            )
    return "", str(location) if location else None, captured_at


def _original_url(html: str) -> str | None:
    for pattern in (_SAVED_FROM, _CANONICAL):
        found = pattern.search(html)
        if found is not None:
            return unescape(found.group("url"))
    return None


class SavedPageCollector:
    """Serve pages from the inbox by exact platform page identity; never fetch anything."""

    def __init__(self, inbox: Path, page_key: PageKey) -> None:
        self._inbox = inbox
        self._page_key = page_key

    def saved_pages(self) -> dict[str, SavedPage]:
        """Rescan the inbox; the newest capture of the same page wins."""

        try:
            require_private_directory(self._inbox)
        except LocalFileSecurityError as error:
            raise SavedPageInboxError() from error
        pages: dict[str, SavedPage] = {}
        candidates = sorted(
            path for path in self._inbox.iterdir() if path.suffix.lower() in SAVED_PAGE_SUFFIXES
        )[:MAXIMUM_SAVED_PAGES]
        for path in candidates:
            page = self._load(path)
            if page is not None and (
                page.key not in pages or page.captured_at > pages[page.key].captured_at
            ):
                pages[page.key] = page
        return pages

    def is_saved(self, url: str) -> bool:
        """Return whether a page matching this URL is in the inbox."""

        key = self._page_key(url)
        return key is not None and key in self.saved_pages()

    async def open(self, url: str, *, screenshot: bool = False) -> BrowserSnapshot:
        """Return the saved page for exactly this URL, or name the page the user should save."""

        del screenshot  # A saved page has no live rendering to capture.
        key = self._page_key(url)
        page = self.saved_pages().get(key) if key is not None else None
        if page is None:
            raise SavedPageMissingError(url)
        title = _TITLE.search(page.html)
        return BrowserSnapshot.model_validate(
            {
                "requested_url": url,
                "final_url": page.url,
                "status_code": 200,
                "title": unescape(title.group("title")).strip()[:500] if title else "",
                "html": page.html,
                "captured_at": page.captured_at,
            }
        )

    def _load(self, path: Path) -> SavedPage | None:
        try:
            details = path.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_size > MAXIMUM_SAVED_PAGE_BYTES:
                return None
            data = path.read_bytes()
        except OSError:
            return None
        captured_at: datetime | None = None
        if path.suffix.lower() in {".mhtml", ".mht"}:
            html, url, captured_at = _read_mhtml(data)
            url = url or _original_url(html)
        else:
            html = _decode(data)
            url = _original_url(html)
        key = self._page_key(url) if url else None
        if url is None or key is None or not html:
            return None
        return SavedPage(
            key=key,
            url=url,
            path=path,
            captured_at=captured_at or datetime.fromtimestamp(details.st_mtime, UTC),
            html=html,
        )
