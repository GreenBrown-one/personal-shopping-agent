"""Unit tests for the strict M1 domain contracts."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import HttpUrl, ValidationError

from personal_shopping_agent.domain import (
    Budget,
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    Money,
    Offer,
    PriceBreakdown,
    PriceKind,
    Product,
    ShoppingCriterion,
    ShoppingRequest,
    Specification,
    StoreType,
)

CAPTURED_AT = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)


def test_money_normalizes_currency_and_rejects_invalid_input() -> None:
    money = Money.model_validate({"amount": "1999.90", "currency": "cny"})

    assert money.amount == Decimal("1999.90")
    assert money.currency == "CNY"

    with pytest.raises(ValidationError):
        Money.model_validate({"amount": "-0.01", "currency": "CNY"})
    with pytest.raises(ValidationError):
        Money.model_validate({"amount": "1", "currency": "CNY", "unexpected": True})


def test_budget_validates_optional_stretch_ceiling() -> None:
    standard = Budget(maximum=Money(amount=Decimal("4000")))
    stretched = Budget(
        maximum=Money(amount=Decimal("4000")),
        stretch_maximum=Money(amount=Decimal("4500")),
    )

    assert standard.stretch_maximum is None
    assert stretched.stretch_maximum == Money(amount=Decimal("4500"))

    with pytest.raises(ValidationError, match="budget currencies must match"):
        Budget(
            maximum=Money(amount=Decimal("4000"), currency="CNY"),
            stretch_maximum=Money(amount=Decimal("600"), currency="USD"),
        )
    with pytest.raises(ValidationError, match="stretch maximum"):
        Budget(
            maximum=Money(amount=Decimal("4000")),
            stretch_maximum=Money(amount=Decimal("3999")),
        )


def test_shopping_request_rejects_duplicate_criteria_and_naive_time() -> None:
    criterion = ShoppingCriterion(
        key="battery_capacity",
        weight=Decimal("2"),
        hard_requirement=True,
        minimum=Decimal("5000"),
        preferred=Decimal("6000"),
        unit="mAh",
    )
    request = ShoppingRequest(
        query="续航优先、性能好的手机",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        criteria=(criterion,),
    )

    assert request.created_at.tzinfo is not None
    assert request.criteria == (criterion,)

    with pytest.raises(ValidationError, match="criterion keys must be unique"):
        ShoppingRequest(
            query="手机",
            category="smartphone",
            budget=request.budget,
            criteria=(criterion, criterion),
        )
    with pytest.raises(ValidationError):
        ShoppingRequest(
            query="手机",
            category="smartphone",
            budget=request.budget,
            created_at=datetime(2026, 8, 9, 10, 0),
        )


@pytest.mark.parametrize(
    ("price_field", "expected_kind"),
    [
        ("estimated_total_cost", PriceKind.ESTIMATED_TOTAL),
        ("unconditional_price", PriceKind.UNCONDITIONAL),
        ("displayed_price", PriceKind.DISPLAYED),
        ("conditional_price", PriceKind.CONDITIONAL),
        ("list_price", PriceKind.LIST),
    ],
)
def test_price_breakdown_uses_documented_fallback_order(
    price_field: str, expected_kind: PriceKind
) -> None:
    price = Money(amount=Decimal("3999"))
    breakdown = PriceBreakdown.model_validate({price_field: price})

    assert breakdown.comparison_price == (expected_kind, price)


def test_price_breakdown_requires_one_currency_and_one_layer() -> None:
    with pytest.raises(ValidationError, match="at least one price layer"):
        PriceBreakdown()
    with pytest.raises(ValidationError, match="same currency"):
        PriceBreakdown(
            displayed_price=Money(amount=Decimal("100"), currency="CNY"),
            estimated_total_cost=Money(amount=Decimal("20"), currency="USD"),
        )


def test_product_offer_and_evidence_keep_distinct_contexts() -> None:
    evidence_id = uuid4()
    specification = Specification(
        key="battery_capacity",
        raw_value="6000mAh",
        normalized_value=Decimal("6000"),
        unit="mAh",
        evidence_ids=(evidence_id,),
    )
    product = Product(
        brand="Example",
        model="X1",
        category="smartphone",
        canonical_name="Example X1",
        identifiers={"manufacturer_model": "X1"},
        specifications=(specification,),
    )
    offer = Offer(
        product_id=product.id,
        platform="JD",
        seller="Example 官方旗舰店",
        store_type=StoreType.BRAND_FLAGSHIP,
        url=HttpUrl("https://example.com/item/1"),
        sku="sku-1",
        variant="12GB+256GB 黑色",
        region="云南省曲靖市",
        captured_at=CAPTURED_AT,
        price=PriceBreakdown(
            displayed_price=Money(amount=Decimal("4299")),
            conditional_price=Money(amount=Decimal("4099")),
            estimated_total_cost=Money(amount=Decimal("4199")),
        ),
        promotion_conditions=("需领券",),
        in_stock=True,
    )
    evidence = Evidence(
        id=evidence_id,
        subject_type=EvidenceSubjectType.SPECIFICATION,
        subject_id=product.id,
        field_path="specifications.battery_capacity",
        source_type=EvidenceSourceType.MANUFACTURER_OFFICIAL,
        source_url=HttpUrl("https://example.com/specifications/x1"),
        source_title="Example X1 官方参数",
        captured_at=CAPTURED_AT,
        observed_value=Decimal("6000"),
        reliability=Decimal("0.95"),
        freshness=Decimal("0.90"),
    )

    assert "price" not in Product.model_fields
    assert offer.product_id == product.id
    assert offer.price.comparison_price[0] is PriceKind.ESTIMATED_TOTAL
    assert evidence.id in product.specifications[0].evidence_ids


def test_evidence_allows_unassessed_confidence_and_tracks_origin_observation() -> None:
    observation_id = uuid4()
    evidence = Evidence(
        subject_type=EvidenceSubjectType.OFFER,
        subject_id=uuid4(),
        field_path="price.displayed_price",
        source_type=EvidenceSourceType.PLATFORM_LISTING,
        source_url=HttpUrl("https://example.com/item/1"),
        source_title="Example listing",
        captured_at=CAPTURED_AT,
        origin_observation_id=observation_id,
        observed_value=Decimal("3999"),
    )

    assert evidence.origin_observation_id == observation_id
    assert evidence.reliability is None
    assert evidence.freshness is None
