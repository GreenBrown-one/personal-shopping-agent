"""Saved browser pages are matched by exact page identity and never fetched."""

import asyncio
import os
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from personal_shopping_agent.sourcing.browser import (
    SavedPageCollector,
    SavedPageInboxError,
    SavedPageMissingError,
    saved_pages,
)
from personal_shopping_agent.sourcing.platforms.jd import jd_page_key

ITEM_URL = "https://item.jd.com/1000001.html"
SEARCH_URL = "https://search.jd.com/Search?keyword=%E5%A4%A7%E7%94%B5%E6%B1%A0&enc=utf-8"


def saved_html(url: str, body: str = "<p>商品</p>", *, title: str = "页面 &amp; 标题") -> str:
    return (
        f"<!DOCTYPE html>\n<!-- saved from url=({len(url):04d}){url} -->\n"
        f"<html><head><title>{title}</title></head><body>{body}</body></html>"
    )


def write(path: Path, text: str, *, encoding: str = "utf-8", mtime: float | None = None) -> Path:
    path.write_bytes(text.encode(encoding))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def mhtml(url: str | None, html: str | None, *, date: str | None) -> bytes:
    message = EmailMessage()
    if url is not None:
        message["Snapshot-Content-Location"] = url
    if date is not None:
        message["Date"] = date
    message.make_mixed()
    if html is not None:
        message.add_attachment(html, subtype="html", cte="quoted-printable")
    else:
        message.add_attachment(b"GIF89a", maintype="image", subtype="gif")
    return message.as_bytes()


def collector(inbox: Path) -> SavedPageCollector:
    return SavedPageCollector(inbox, jd_page_key)


def test_html_pages_are_served_by_their_saved_url_with_file_time(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    write(inbox / "item.html", saved_html(ITEM_URL), mtime=1_790_000_000)

    snapshot = asyncio.run(collector(inbox).open(ITEM_URL))

    assert str(snapshot.final_url) == ITEM_URL
    assert snapshot.status_code == 200
    assert snapshot.title == "页面 & 标题"
    assert "<p>商品</p>" in snapshot.html
    assert snapshot.captured_at == datetime.fromtimestamp(1_790_000_000, UTC)
    assert collector(inbox).is_saved(ITEM_URL)
    assert not collector(inbox).is_saved("https://item.jd.com/2.html")
    assert not collector(inbox).is_saved("https://example.com/")


def test_canonical_links_gbk_pages_and_missing_titles_are_supported(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    page = f'<html><head><link rel="canonical" href="{ITEM_URL}"></head><body>中文</body></html>'
    inbox.mkdir(mode=0o700)
    write(inbox / "gbk.htm", page, encoding="gb18030")

    snapshot = asyncio.run(collector(inbox).open(ITEM_URL))

    assert snapshot.title == ""
    assert "中文" in snapshot.html


def test_mhtml_uses_snapshot_location_and_date(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    (inbox / "search.mhtml").write_bytes(
        mhtml(
            SEARCH_URL,
            "<html><title>搜索</title>结果</html>",
            date="Fri, 25 Sep 2026 10:00:00 +0800",
        )
    )

    snapshot = asyncio.run(collector(inbox).open(SEARCH_URL))

    assert snapshot.title == "搜索"
    assert snapshot.captured_at == datetime(2026, 9, 25, 2, 0, tzinfo=UTC)


def test_mhtml_without_location_falls_back_to_the_html_and_file_time(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    raw = b"Date: not a date\n" + mhtml(None, saved_html(ITEM_URL), date=None)
    (inbox / "item.mht").write_bytes(raw)

    snapshot = asyncio.run(collector(inbox).open(ITEM_URL))

    assert snapshot.captured_at.tzinfo is not None


def test_the_newest_capture_of_the_same_page_wins(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    write(inbox / "a.html", saved_html(ITEM_URL, "旧"), mtime=1_700_000_000)
    write(inbox / "b.html", saved_html(ITEM_URL, "新"), mtime=1_800_000_000)
    write(inbox / "c.html", saved_html(ITEM_URL, "更旧"), mtime=1_600_000_000)

    assert "新" in asyncio.run(collector(inbox).open(ITEM_URL)).html


def test_unusable_files_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    target = write(tmp_path / "outside.html", saved_html(ITEM_URL))
    (inbox / "link.html").symlink_to(target)
    (inbox / "folder.html").mkdir()
    write(inbox / "notes.txt", saved_html(ITEM_URL))
    write(inbox / "unknown.html", "<html>no source url</html>")
    write(inbox / "other-site.html", saved_html("https://example.com/item/1"))
    (inbox / "no-html.mhtml").write_bytes(mhtml(ITEM_URL, None, date=None))
    (inbox / "bad-bytes.html").write_bytes(b"\xff\xfe\x81" + saved_html(ITEM_URL).encode())
    write(inbox / "unreadable.html", saved_html(ITEM_URL))
    monkeypatch.setattr(saved_pages, "MAXIMUM_SAVED_PAGE_BYTES", 10_000)
    write(inbox / "huge.html", saved_html(ITEM_URL, "x" * 20_000))

    original = Path.read_bytes

    def fail_one(self: Path) -> bytes:
        if self.name == "unreadable.html":
            raise PermissionError("locked")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", fail_one)
    pages = collector(inbox).saved_pages()

    assert list(pages) == ["item:1000001"]
    assert pages["item:1000001"].path.name == "bad-bytes.html"


def test_a_missing_page_names_the_exact_url_to_save(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"

    with pytest.raises(SavedPageMissingError) as missing:
        asyncio.run(collector(inbox).open(ITEM_URL))

    assert missing.value.code == "page_not_saved"
    assert missing.value.url == ITEM_URL
    assert ITEM_URL in str(missing.value)
    assert inbox.is_dir()  # created privately on first use
    with pytest.raises(SavedPageMissingError):
        asyncio.run(collector(inbox).open("https://example.com/not-a-jd-page"))


@pytest.mark.parametrize(
    ("url", "key"),
    [
        (ITEM_URL, "item:1000001"),
        ("https://ITEM.jd.com/1000001.html?from=search#x", "item:1000001"),
        ("https://item.jd.com/1000001.htm", None),
        (SEARCH_URL, "search:大电池"),
        ("https://search.jd.com/Search?keyword=%20a%20%20b%20", "search:a b"),
        ("https://search.jd.com/Search?enc=utf-8", None),
        ("https://www.jd.com/", None),
        ("http://[::1", None),
    ],
)
def test_jd_page_identity(url: str, key: str | None) -> None:
    assert jd_page_key(url) == key


@pytest.mark.parametrize(
    "kind",
    [
        "file",
        pytest.param(
            "shared",
            marks=pytest.mark.skipif(
                os.name != "posix", reason="owner-only mode bits are enforced only on POSIX hosts"
            ),
        ),
    ],
)
def test_a_shared_inbox_is_refused_with_an_actionable_message(tmp_path: Path, kind: str) -> None:
    inbox = tmp_path / "inbox"
    if kind == "file":
        inbox.write_text("not a directory")
    else:
        inbox.mkdir()
        inbox.chmod(0o755)

    with pytest.raises(SavedPageInboxError) as refused:
        collector(inbox).saved_pages()

    assert refused.value.code == "inbox_not_private"
    assert "chmod 700" in str(refused.value)
