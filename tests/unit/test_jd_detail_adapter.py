"""Fixture-driven JD product-detail tests with no live platform requests."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl

from personal_shopping_agent.domain import StoreType
from personal_shopping_agent.sourcing import (
    PlatformAccessRestrictedError,
    PlatformCandidate,
    PlatformDetailParseError,
)
from personal_shopping_agent.sourcing.platforms import JDDetailAdapter
from personal_shopping_agent.sourcing.platforms.jd_detail import (
    classify_jd_store,
    normalize_jd_stock,
)

CAPTURED_AT = datetime(2026, 8, 9, 14, 0, tzinfo=UTC)
FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "jd" / "product_detail.html"


def candidate(
    *,
    platform: str = "jd",
    external_id: str = "1000001",
    product_url: str = "https://item.jd.com/1000001.html",
) -> PlatformCandidate:
    return PlatformCandidate(
        platform=platform,
        external_id=external_id,
        title="Search result title",
        product_url=HttpUrl(product_url),
    )


class DetailPage:
    def __init__(
        self,
        html: str = FIXTURE_PATH.read_text(encoding="utf-8"),
        final_url: str = "https://item.jd.com/1000001.html",
    ) -> None:
        self.final_url = HttpUrl(final_url)
        self.html = html
        self.captured_at = CAPTURED_AT


def test_detail_parser_preserves_price_variant_stock_and_specification_context() -> None:
    result = JDDetailAdapter().parse_detail(candidate(), DetailPage())

    assert result.platform == "jd"
    assert result.external_id == "1000001"
    assert result.title == "Example Aurora Phone A1 12GB+256GB"
    assert str(result.product_url) == "https://item.jd.com/1000001.html"
    assert result.captured_at == CAPTURED_AT
    assert result.brand == "Example"
    assert result.model == "A1-256"
    assert result.seller_name == "Example 品牌旗舰店"
    assert result.store_type is StoreType.BRAND_FLAGSHIP
    assert result.store_type_basis == "品牌旗舰店 Example 品牌旗舰店"
    assert str(result.list_price) == "4299.00"
    assert str(result.displayed_price) == "3999.00"
    assert str(result.conditional_price) == "3799.00"
    assert result.promotion_conditions == (
        "需在商品页领取满200减200券",
        "优惠以结算前人工确认为准",
    )
    assert result.selected_variant == "颜色:星夜黑; 存储容量:12GB+256GB"
    assert len(result.variant_options) == 4
    assert [option.selected for option in result.variant_options] == [True, False, True, False]
    assert result.region == "云南省 曲靖市 麒麟区"
    assert result.stock_status == "有货"
    assert result.in_stock is True
    assert not result.is_off_shelf
    assert [(item.key, item.raw_value) for item in result.specifications] == [
        ("品牌", "Example"),
        ("型号", "A1-256"),
        ("电池容量", "6000mAh"),
    ]


def test_detail_parser_keeps_missing_commercial_fields_unknown_and_marks_off_shelf() -> None:
    html = """
    <div class="itemInfo-wrap" data-sku="1000001">
      <div class="sku-name">Archived Example</div>
      <div class="store-prompt">配送信息待确认</div>
      <span class="p-price" data-price-kind="displayed">暂无报价</span>
      <ul class="parameter2"><li>说明\uff1a   </li></ul>
      该商品已下柜, 欢迎挑选其他商品!
    </div>
    """

    result = JDDetailAdapter().parse_detail(candidate(), DetailPage(html=html))

    assert result.seller_name is None
    assert result.store_type is StoreType.UNKNOWN
    assert result.store_type_basis is None
    assert result.list_price is None
    assert result.displayed_price is None
    assert result.conditional_price is None
    assert result.promotion_conditions == ()
    assert result.selected_variant is None
    assert result.variant_options == ()
    assert result.region is None
    assert result.stock_status == "配送信息待确认"
    assert result.in_stock is False
    assert result.is_off_shelf
    assert result.brand is None
    assert result.model is None
    assert result.specifications == ()


@pytest.mark.parametrize("marker", ["请输入验证码", "安全验证", "访问过于频繁", "CAPTCHA"])
def test_detail_parser_stops_for_human_verification(marker: str) -> None:
    with pytest.raises(PlatformAccessRestrictedError) as captured:
        JDDetailAdapter().parse_detail(
            candidate(),
            DetailPage(html=f"<div>{marker}</div>"),
        )

    assert captured.value.code == "jd_access_restricted"


def test_detail_parser_rejects_non_jd_candidate() -> None:
    with pytest.raises(PlatformDetailParseError) as captured:
        JDDetailAdapter().parse_detail(candidate(platform="taobao"), DetailPage())
    assert captured.value.code == "platform_mismatch"


@pytest.mark.parametrize(
    "final_url",
    [
        "https://example.com/1000001.html",
        "https://item.jd.com/other.html",
        "https://item.jd.com:444/1000001.html",
    ],
)
def test_detail_parser_requires_exact_jd_item_url(final_url: str) -> None:
    with pytest.raises(PlatformDetailParseError) as captured:
        JDDetailAdapter().parse_detail(candidate(), DetailPage(final_url=final_url))
    assert captured.value.code == "detail_url_invalid"


def test_detail_parser_rejects_candidate_url_mismatch() -> None:
    with pytest.raises(PlatformDetailParseError) as captured:
        JDDetailAdapter().parse_detail(
            candidate(external_id="1000002"),
            DetailPage(final_url="https://item.jd.com/1000002.html"),
        )
    assert captured.value.code == "candidate_url_mismatch"


def test_detail_parser_rejects_dom_sku_mismatch() -> None:
    html = '<div class="itemInfo-wrap" data-sku="999"><div class="sku-name">Title</div></div>'
    with pytest.raises(PlatformDetailParseError) as captured:
        JDDetailAdapter().parse_detail(candidate(), DetailPage(html=html))
    assert captured.value.code == "detail_sku_mismatch"


def test_detail_parser_requires_detail_title() -> None:
    html = '<div class="itemInfo-wrap" data-sku="1000001"></div>'
    with pytest.raises(PlatformDetailParseError) as captured:
        JDDetailAdapter().parse_detail(candidate(), DetailPage(html=html))
    assert captured.value.code == "detail_title_missing"


def test_truncated_detail_capture_is_finalized() -> None:
    html = '<div class="itemInfo-wrap" data-sku="1000001"><div class="sku-name">Truncated'

    result = JDDetailAdapter().parse_detail(candidate(), DetailPage(html=html))

    assert result.title == "Truncated"


@pytest.mark.parametrize(
    ("seller", "label", "expected_type", "expected_basis"),
    [
        (
            "Example 京东自营旗舰店",
            None,
            StoreType.PLATFORM_SELF_OPERATED,
            "Example 京东自营旗舰店",
        ),
        ("Example Shop", "官方旗舰店", StoreType.BRAND_FLAGSHIP, "官方旗舰店 Example Shop"),
        ("Example 官方旗舰店", None, StoreType.BRAND_FLAGSHIP, "Example 官方旗舰店"),
        ("Example Shop", "第三方店铺", StoreType.UNKNOWN, "第三方店铺 Example Shop"),
        (None, None, StoreType.UNKNOWN, None),
    ],
)
def test_store_classification_requires_explicit_evidence(
    seller: str | None,
    label: str | None,
    expected_type: StoreType,
    expected_basis: str | None,
) -> None:
    assert classify_jd_store(seller, label) == (expected_type, expected_basis)


@pytest.mark.parametrize(
    ("status", "off_shelf", "expected"),
    [
        (None, True, False),
        ("暂时无货", False, False),
        ("现货, 可配送", False, True),
        ("有货", False, True),
        ("配送信息待确认", False, None),
        (None, False, None),
    ],
)
def test_stock_normalization_preserves_ambiguous_status(
    status: str | None, off_shelf: bool, expected: bool | None
) -> None:
    assert normalize_jd_stock(status, off_shelf=off_shelf) is expected


def test_detail_parser_ignores_empty_and_invalid_capture_shapes() -> None:
    html = """
    <div class="itemInfo-wrap" data-sku="1000001">
      <div class="sku-name">Minimal Example</div>
      <img alt="void element"></img>
      <div class="prom-item">   </div>
      <div class="choose-attr" data-type="颜色">
        <div class="item" data-sku="" data-value=""></div>
      </div>
      <ul class="parameter2">
        <li>没有分隔符</li>
        <li>\uff1avalue</li>
        <li>key\uff1a</li>
      </ul>
    """

    result = JDDetailAdapter().parse_detail(candidate(), DetailPage(html=html))

    assert result.title == "Minimal Example"
    assert result.promotion_conditions == ()
    assert result.variant_options == ()
    assert result.specifications == ()
