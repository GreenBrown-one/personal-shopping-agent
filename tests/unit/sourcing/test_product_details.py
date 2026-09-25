"""Unit tests for provider-neutral detail collection orchestration."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl

from personal_shopping_agent.sourcing import (
    PlatformCandidate,
    ProductDetailService,
)
from personal_shopping_agent.sourcing.platforms import JDDetailAdapter

CAPTURED_AT = datetime(2026, 8, 9, 14, 0, tzinfo=UTC)
FIXTURE_PATH = Path(__file__).parents[2] / "fixtures" / "jd" / "product_detail.html"


def jd_candidate(*, platform: str = "jd") -> PlatformCandidate:
    return PlatformCandidate(
        platform=platform,
        external_id="1000001",
        title="Search title",
        product_url=HttpUrl("https://item.jd.com/1000001.html"),
    )


class DetailPage:
    final_url = HttpUrl("https://item.jd.com/1000001.html")
    html = FIXTURE_PATH.read_text(encoding="utf-8")
    captured_at = CAPTURED_AT


class DetailCollector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def open(self, url: str, *, screenshot: bool = False) -> DetailPage:
        self.calls.append((url, screenshot))
        return DetailPage()


def test_detail_service_collects_exact_candidate_url() -> None:
    collector = DetailCollector()
    service = ProductDetailService(collector, JDDetailAdapter())

    result = asyncio.run(service.collect(jd_candidate(), screenshot=True))

    assert collector.calls == [("https://item.jd.com/1000001.html", True)]
    assert result.external_id == "1000001"
    assert result.title == "Example Aurora Phone A1 12GB+256GB"


def test_detail_service_rejects_platform_mismatch_before_collection() -> None:
    collector = DetailCollector()
    service = ProductDetailService(collector, JDDetailAdapter())

    with pytest.raises(ValueError, match="platform"):
        asyncio.run(service.collect(jd_candidate(platform="taobao")))

    assert collector.calls == []
