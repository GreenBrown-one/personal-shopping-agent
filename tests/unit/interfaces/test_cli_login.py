"""The login command only opens a visible browser; the user signs in themselves."""

import io
import json

import pytest

from personal_shopping_agent.interfaces.cli import (
    SIGN_IN_INSTRUCTIONS,
    create_sign_in_browser,
    main,
    wait_for_enter,
)
from personal_shopping_agent.sourcing.browser import (
    BrowserManager,
    BrowserManagerError,
    NavigationPolicyError,
)
from personal_shopping_agent.sourcing.platforms.jd import JD_SIGN_IN_URL


class FakeSignInBrowser:
    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        open_error: Exception | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._start_error = start_error
        self._open_error = open_error

    async def start(self) -> None:
        self.calls.append("start")
        if self._start_error is not None:
            raise self._start_error

    async def open(self, url: str, *, screenshot: bool = False) -> object:
        assert screenshot is False
        self.calls.append(f"open:{url}")
        if self._open_error is not None:
            raise self._open_error
        return object()

    async def close(self) -> None:
        self.calls.append("close")


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_login_opens_the_sign_in_page_waits_for_the_user_and_closes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    browser = FakeSignInBrowser()
    waits: list[str] = []

    exit_code = main(
        ["login", "jd"],
        sign_in_browser_factory=lambda: browser,
        wait_for_user=lambda: waits.append("enter"),
    )

    assert exit_code == 0
    assert browser.calls == ["start", f"open:{JD_SIGN_IN_URL}", "close"]
    assert waits == ["enter"]
    payload = _output(capsys)
    assert payload["ok"] is True
    assert payload["platform"] == "jd"


@pytest.mark.parametrize(
    ("browser", "code", "expected_calls"),
    [
        (
            FakeSignInBrowser(start_error=BrowserManagerError("raw launch detail")),
            "browser_unavailable",
            ["start"],
        ),
        (
            FakeSignInBrowser(
                open_error=NavigationPolicyError("host_not_allowed", "raw redirect detail")
            ),
            "sign_in_page_unavailable",
            ["start", f"open:{JD_SIGN_IN_URL}", "close"],
        ),
    ],
)
def test_login_failures_are_sanitized_and_always_release_the_browser(
    capsys: pytest.CaptureFixture[str],
    browser: FakeSignInBrowser,
    code: str,
    expected_calls: list[str],
) -> None:
    exit_code = main(
        ["login", "jd"],
        sign_in_browser_factory=lambda: browser,
        wait_for_user=lambda: pytest.fail("must not wait when the page is unavailable"),
    )

    raw = capsys.readouterr().out
    assert exit_code == 2
    assert "raw" not in raw
    assert json.loads(raw)["error_code"] == code
    assert browser.calls == expected_calls


def test_login_rejects_unknown_platforms() -> None:
    with pytest.raises(SystemExit):
        main(["login", "taobao"])


def test_default_sign_in_browser_is_visible_and_guarded() -> None:
    browser = create_sign_in_browser()

    assert isinstance(browser, BrowserManager)
    assert browser.settings.headless is False


def test_wait_for_enter_prints_instructions_to_stderr_and_reads_one_line(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))

    wait_for_enter()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == SIGN_IN_INSTRUCTIONS
