"""Fixture-based JD search parser tests; no live shopping site is contacted."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl

from personal_shopping_agent.sourcing import (
    PlatformAccessRestrictedError,
)
from personal_shopping_agent.sourcing.platforms import JDSearchAdapter
from personal_shopping_agent.sourcing.platforms.jd import normalize_jd_item_url

CAPTURED_AT = datetime(2026, 8, 9, 13, 0, tzinfo=UTC)
FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "jd" / "search_results.html"


class FixturePage:
    def __init__(
        self,
        html: str = FIXTURE_PATH.read_text(encoding="utf-8"),
        final_url: str = "https://search.jd.com/Search?keyword=phone&enc=utf-8",
    ) -> None:
        self.final_url = HttpUrl(final_url)
        self.html = html
        self.captured_at = CAPTURED_AT


def test_build_search_url_normalizes_and_encodes_query() -> None:
    adapter = JDSearchAdapter()

    assert adapter.platform == "jd"
    assert adapter.build_search_url("  手机   续航  ") == (
        "https://search.jd.com/Search?keyword=%E6%89%8B%E6%9C%BA+%E7%BB%AD%E8%88%AA&enc=utf-8"
    )


@pytest.mark.parametrize("query", ["", "   ", "x" * 201])
def test_build_search_url_rejects_invalid_queries(query: str) -> None:
    with pytest.raises(ValueError, match="query"):
        JDSearchAdapter().build_search_url(query)


def test_parse_search_preserves_observed_fields_without_promoting_identity() -> None:
    result = JDSearchAdapter().parse_search("example phone", FixturePage(), maximum_candidates=20)

    assert result.platform == "jd"
    assert result.query == "example phone"
    assert str(result.source_url).startswith("https://search.jd.com/Search?")
    assert result.captured_at == CAPTURED_AT
    assert [candidate.external_id for candidate in result.candidates] == ["1000001", "1000002"]

    first, second = result.candidates
    assert first.title == "Example Aurora Phone A1"
    assert str(first.product_url) == "https://item.jd.com/1000001.html"
    assert str(first.displayed_price) == "3999.00"
    assert first.seller_name == "Example Digital Store"
    assert first.review_summary == "1万+条评价"
    assert first.badges == ("自营", "放心购")

    assert second.title == "Example Meadow Phone B2"
    assert second.displayed_price is None
    assert second.seller_name is None
    assert second.review_summary is None
    assert second.badges == ()


def test_parse_search_honors_candidate_limit() -> None:
    result = JDSearchAdapter().parse_search("phone", FixturePage(), maximum_candidates=1)

    assert len(result.candidates) == 1


@pytest.mark.parametrize("limit", [0, 101])
def test_parse_search_rejects_unbounded_limits(limit: int) -> None:
    with pytest.raises(ValueError, match="maximum_candidates"):
        JDSearchAdapter().parse_search("phone", FixturePage(), maximum_candidates=limit)


def test_parse_search_requires_the_jd_search_host() -> None:
    with pytest.raises(ValueError, match=r"search\.jd\.com"):
        JDSearchAdapter().parse_search(
            "phone",
            FixturePage(final_url="https://example.com/search"),
            maximum_candidates=20,
        )


@pytest.mark.parametrize("marker", ["请输入验证码", "安全验证", "访问过于频繁", "CAPTCHA"])
def test_parse_search_stops_on_human_verification(marker: str) -> None:
    with pytest.raises(PlatformAccessRestrictedError) as captured:
        JDSearchAdapter().parse_search(
            "phone",
            FixturePage(html=f"<html><body>{marker}</body></html>"),
            maximum_candidates=20,
        )

    assert captured.value.code == "jd_access_restricted"


@pytest.mark.parametrize(
    ("link", "sku", "expected"),
    [
        (None, "100", None),
        ("//item.jd.com/100.html", "100", "https://item.jd.com/100.html"),
        ("http://item.jd.com/100.html", "100", None),
        ("https://evil.example/100.html", "100", None),
        ("https://item.jd.com:444/100.html", "100", None),
        ("https://user@item.jd.com/100.html", "100", None),
        ("https://user:secret@item.jd.com/100.html", "100", None),
        ("https://item.jd.com/other.html", "100", None),
        ("https://item.jd.com:invalid/100.html", "100", None),
    ],
)
def test_item_url_accepts_only_the_exact_public_product_shape(
    link: str | None, sku: str, expected: str | None
) -> None:
    assert normalize_jd_item_url(link, sku) == expected


def test_parser_finalizes_a_truncated_fixture_and_ignores_empty_badge() -> None:
    html = """
    </orphan>
    <li class="gl-item" data-sku="2000001">
      <a href="//item.jd.com/2000001.html">outside capture</a>
      <img alt="void tag">
      <div class="p-name">Truncated Example</div>
      <i class="goods-icons">   </i>
    """

    result = JDSearchAdapter().parse_search(
        "truncated", FixturePage(html=html), maximum_candidates=20
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].external_id == "2000001"
    assert result.candidates[0].badges == ()
