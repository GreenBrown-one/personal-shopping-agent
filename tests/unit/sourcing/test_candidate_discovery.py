"""Unit tests for the provider-neutral candidate discovery use case."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from pydantic import HttpUrl

from personal_shopping_agent.sourcing import (
    CandidateDiscoveryService,
)
from personal_shopping_agent.sourcing.platforms import JDSearchAdapter

CAPTURED_AT = datetime(2026, 8, 9, 13, 0, tzinfo=UTC)
FIXTURE_PATH = Path(__file__).parents[2] / "fixtures" / "jd" / "search_results.html"


class FixturePage:
    final_url = HttpUrl("https://search.jd.com/Search?keyword=phone&enc=utf-8")
    html = FIXTURE_PATH.read_text(encoding="utf-8")
    captured_at = CAPTURED_AT


class FixtureCollector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def open(self, url: str, *, screenshot: bool = False) -> FixturePage:
        self.calls.append((url, screenshot))
        return FixturePage()


def test_service_collects_once_and_delegates_to_selected_adapter() -> None:
    collector = FixtureCollector()
    service = CandidateDiscoveryService(collector, JDSearchAdapter())

    result = asyncio.run(
        service.discover("  example   phone  ", maximum_candidates=1, screenshot=True)
    )

    assert collector.calls == [
        ("https://search.jd.com/Search?keyword=example+phone&enc=utf-8", True)
    ]
    assert result.platform == "jd"
    assert result.query == "example phone"
    assert len(result.candidates) == 1
