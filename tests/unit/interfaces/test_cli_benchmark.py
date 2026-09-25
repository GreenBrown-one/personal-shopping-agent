"""benchmark refresh/show read one public page and keep the result private and local."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from personal_shopping_agent.infrastructure.settings import BENCHMARK_FILE_ENV
from personal_shopping_agent.interfaces.cli import create_benchmark_browser, main
from personal_shopping_agent.interfaces.composition import create_benchmark_policy
from personal_shopping_agent.interfaces.mcp.server import load_chip_benchmark_providers
from personal_shopping_agent.sourcing.browser import (
    BrowserManager,
    BrowserManagerError,
    BrowserSnapshot,
)
from personal_shopping_agent.sourcing.platforms.socpk import SOCPK_OVERALL_URL

FIXTURE = Path(__file__).parents[2] / "fixtures" / "socpk" / "chip_overall_rendered.html"


class FakeBenchmarkBrowser:
    def __init__(self, html: str, *, open_error: Exception | None = None) -> None:
        self.html = html
        self.open_error = open_error
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def open(self, url: str, *, screenshot: bool = False) -> BrowserSnapshot:
        self.calls.append(f"open:{url}")
        if self.open_error is not None:
            raise self.open_error
        return BrowserSnapshot.model_validate(
            {
                "requested_url": url,
                "final_url": url,
                "status_code": 200,
                "title": "综合性能排行",
                "html": self.html,
                "captured_at": datetime(2026, 9, 1, tzinfo=UTC),
            }
        )

    async def close(self) -> None:
        self.calls.append("close")


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_refresh_saves_privately_then_show_lists_scores_and_a_suggestion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = {BENCHMARK_FILE_ENV: str(tmp_path / "benchmarks" / "socpk.json")}
    browser = FakeBenchmarkBrowser(FIXTURE.read_text(encoding="utf-8"))

    assert main(["benchmark", "show"], environment=environment) == 1
    assert _output(capsys)["error_code"] == "benchmark_missing"

    exit_code = main(
        ["benchmark", "refresh"],
        environment=environment,
        benchmark_browser_factory=lambda: browser,
    )

    refreshed = _output(capsys)
    assert exit_code == 0
    assert browser.calls == ["start", f"open:{SOCPK_OVERALL_URL}", "close"]
    assert refreshed["chips"] == 22
    assert refreshed["suggested_criterion"] == {
        "key": "chip_performance",
        "minimum": "100",
        "preferred": "450.0",
        "unit": "SOCPK",
    }

    assert main(["benchmark", "show", "--filter", "骁龙 8 Gen"], environment=environment) == 0
    shown = _output(capsys)
    names = [item["name"] for item in cast(list[dict[str, str]], shown["entries"])]
    assert names[:2] == ["骁龙 8 Gen3", "骁龙 8 Gen2"]
    assert all("骁龙8" in name.replace(" ", "") for name in names)
    assert main(["benchmark", "show"], environment=environment) == 0
    assert len(cast(list[object], _output(capsys)["entries"])) == 22
    assert len(load_chip_benchmark_providers(environment)) == 1


@pytest.mark.parametrize(
    ("browser", "code"),
    [
        (
            FakeBenchmarkBrowser("", open_error=BrowserManagerError("raw detail")),
            "benchmark_page_unavailable",
        ),
        (FakeBenchmarkBrowser("<html>layout changed</html>"), "benchmark_page_unrecognized"),
    ],
)
def test_refresh_failures_save_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    browser: FakeBenchmarkBrowser,
    code: str,
) -> None:
    path = tmp_path / "benchmarks" / "socpk.json"
    exit_code = main(
        ["benchmark", "refresh"],
        environment={BENCHMARK_FILE_ENV: str(path)},
        benchmark_browser_factory=lambda: browser,
    )

    raw = capsys.readouterr().out
    assert exit_code == 2
    assert json.loads(raw)["error_code"] == code
    assert "raw detail" not in raw
    assert browser.calls[-1] == "close"
    assert not path.exists()


def test_store_failures_are_reported_and_disable_chip_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    browser = FakeBenchmarkBrowser(FIXTURE.read_text(encoding="utf-8"))
    assert (
        main(
            ["benchmark", "refresh"],
            environment={BENCHMARK_FILE_ENV: str(blocker / "socpk.json")},
            benchmark_browser_factory=lambda: browser,
        )
        == 2
    )
    assert _output(capsys)["error_code"] == "benchmark_store_failed"

    unreadable = tmp_path / "private" / "socpk.json"
    unreadable.parent.mkdir(mode=0o700)
    unreadable.write_text("{}", encoding="utf-8")
    unreadable.chmod(0o600)
    environment = {BENCHMARK_FILE_ENV: str(unreadable)}
    assert main(["benchmark", "show"], environment=environment) == 2
    assert _output(capsys)["error_code"] == "benchmark_unreadable"
    assert load_chip_benchmark_providers(environment) == ()
    assert load_chip_benchmark_providers({BENCHMARK_FILE_ENV: str(tmp_path / "none.json")}) == ()


def test_benchmark_browser_is_headless_isolated_and_limited_to_socpk() -> None:
    browser = create_benchmark_browser()

    assert isinstance(browser, BrowserManager)
    assert browser.settings.headless is True
    assert browser.settings.profile_directory == Path("data/browser-profiles/benchmarks")
    assert create_benchmark_policy().allowed_hosts == {"www.socpk.com", "socpk.com"}
