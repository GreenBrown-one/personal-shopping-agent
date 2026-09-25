"""Unit tests for explainable criterion utility and offer-cost foundations."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import HttpUrl, ValidationError

from personal_shopping_agent.domain import (
    Budget,
    InvalidCriterionDefinitionError,
    Money,
    Offer,
    PriceBreakdown,
    PriceKind,
    Product,
    ShoppingCriterion,
    ShoppingRequest,
    StoreType,
)
from personal_shopping_agent.presentation import (
    CandidateScoringFoundation,
    CandidateScoringFoundationBuilder,
    CriterionEvaluation,
    CriterionEvaluationStatus,
    CriterionEvaluator,
    OfferCostAssessment,
    OfferCostEstimator,
)
from personal_shopping_agent.sourcing import (
    NormalizedSpecification,
    SpecificationNormalizationStatus,
)

NOW = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)


def build_product(*, product_id: UUID | None = None) -> Product:
    return Product(
        id=product_id or uuid4(),
        brand="Example",
        model="X1",
        category="smartphone",
        canonical_name="Example X1",
    )


def build_request(*criteria: ShoppingCriterion, request_id: UUID | None = None) -> ShoppingRequest:
    return ShoppingRequest(
        id=request_id or uuid4(),
        query="续航好且重量合适的手机",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        criteria=criteria,
        created_at=NOW,
    )


def build_fact(
    request_id: UUID,
    product_id: UUID,
    key: str,
    *values: Decimal,
    status: SpecificationNormalizationStatus = SpecificationNormalizationStatus.NORMALIZED,
) -> NormalizedSpecification:
    unsupported = status is SpecificationNormalizationStatus.UNSUPPORTED_KEY
    return NormalizedSpecification(
        request_id=request_id,
        workflow_id=uuid4(),
        product_id=product_id,
        canonical_key=key,
        source_field_paths=(f"specifications.{key}",),
        raw_values=("raw",),
        normalized_values=values,
        canonical_unit=None if unsupported else "unit",
        evidence_ids=(uuid4(),),
        status=status,
        normalized_at=NOW,
    )


def build_offer(
    product_id: UUID,
    *,
    offer_id: UUID | None = None,
    amount: str = "100",
    price_kind: PriceKind = PriceKind.ESTIMATED_TOTAL,
    currency: str = "CNY",
    store_type: StoreType = StoreType.PLATFORM_SELF_OPERATED,
    in_stock: bool | None = True,
    region: str | None = "云南省曲靖市",
    promotion_conditions: tuple[str, ...] = ("需领券",),
) -> Offer:
    field_by_kind = {
        PriceKind.LIST: "list_price",
        PriceKind.DISPLAYED: "displayed_price",
        PriceKind.UNCONDITIONAL: "unconditional_price",
        PriceKind.CONDITIONAL: "conditional_price",
        PriceKind.ESTIMATED_TOTAL: "estimated_total_cost",
    }
    return Offer(
        id=offer_id or uuid4(),
        product_id=product_id,
        platform="JD",
        seller="Example store",
        store_type=store_type,
        url=HttpUrl("https://item.example.com/1"),
        region=region,
        captured_at=NOW,
        price=PriceBreakdown.model_validate(
            {field_by_kind[price_kind]: Money(amount=Decimal(amount), currency=currency)}
        ),
        promotion_conditions=promotion_conditions,
        in_stock=in_stock,
    )


def build_evaluation(
    *,
    key: str = "battery_capacity",
    hard_requirement: bool = False,
    status: CriterionEvaluationStatus = CriterionEvaluationStatus.SATISFIED,
    score: str = "1",
    values: tuple[Decimal, ...] = (Decimal("5000"),),
) -> CriterionEvaluation:
    return CriterionEvaluation(
        key=key,
        weight=Decimal("1"),
        hard_requirement=hard_requirement,
        observed_values=values,
        status=status,
        score=Decimal(score),
        reason_code="test",
    )


def build_cost(
    product_id: UUID,
    *,
    offer_id: UUID | None = None,
    amount: str = "100",
    risk: str = "0",
    comparable: bool = True,
) -> OfferCostAssessment:
    price = Money(amount=Decimal(amount))
    coefficient = Decimal(risk)
    return OfferCostAssessment(
        offer_id=offer_id or uuid4(),
        product_id=product_id,
        selected_price_kind=PriceKind.ESTIMATED_TOTAL,
        selected_price=price,
        risk_coefficient=coefficient,
        effective_cost=Money(amount=price.amount * (Decimal("1") + coefficient)),
        comparable=comparable,
        reason_codes=("test",),
    )


@pytest.mark.parametrize(
    ("criterion", "value", "expected_score", "expected_status", "reason"),
    (
        (
            ShoppingCriterion(
                key="battery_capacity",
                minimum=Decimal("5000"),
                preferred=Decimal("6000"),
                unit="unit",
            ),
            "4000",
            "0.400000",
            CriterionEvaluationStatus.UNSATISFIED,
            "below_minimum",
        ),
        (
            ShoppingCriterion(
                key="battery_capacity",
                minimum=Decimal("5000"),
                preferred=Decimal("6000"),
                unit="unit",
            ),
            "5500",
            "0.750000",
            CriterionEvaluationStatus.SATISFIED,
            "below_preferred",
        ),
        (
            ShoppingCriterion(
                key="battery_capacity",
                minimum=Decimal("5000"),
                preferred=Decimal("6000"),
                unit="unit",
            ),
            "6000",
            "1.000000",
            CriterionEvaluationStatus.SATISFIED,
            "preferred_met",
        ),
        (
            ShoppingCriterion(
                key="weight", preferred=Decimal("180"), maximum=Decimal("200"), unit="unit"
            ),
            "190",
            "0.750000",
            CriterionEvaluationStatus.SATISFIED,
            "above_preferred",
        ),
        (
            ShoppingCriterion(
                key="weight", preferred=Decimal("180"), maximum=Decimal("200"), unit="unit"
            ),
            "220",
            "0.454545",
            CriterionEvaluationStatus.UNSATISFIED,
            "above_maximum",
        ),
        (
            ShoppingCriterion(
                key="weight", minimum=Decimal("150"), maximum=Decimal("250"), unit="unit"
            ),
            "200",
            "1",
            CriterionEvaluationStatus.SATISFIED,
            "within_bounds",
        ),
        (
            ShoppingCriterion(
                key="weight",
                minimum=Decimal("150"),
                preferred=Decimal("200"),
                maximum=Decimal("250"),
                unit="unit",
            ),
            "225",
            "0.750000",
            CriterionEvaluationStatus.SATISFIED,
            "above_preferred",
        ),
    ),
)
def test_evaluator_scores_explicit_numeric_bounds(
    criterion: ShoppingCriterion,
    value: str,
    expected_score: str,
    expected_status: CriterionEvaluationStatus,
    reason: str,
) -> None:
    request = build_request(criterion)
    product = build_product()

    result = CriterionEvaluator().evaluate(
        request.criteria,
        (build_fact(request.id, product.id, criterion.key, Decimal(value)),),
    )[0]

    assert result.score == Decimal(expected_score)
    assert result.status is expected_status
    assert result.reason_code == reason


def test_evaluator_keeps_missing_conflicting_and_unresolved_facts_explicit() -> None:
    criteria = (
        ShoppingCriterion(key="missing", minimum=Decimal("1")),
        ShoppingCriterion(key="conflict", minimum=Decimal("1")),
        ShoppingCriterion(key="multi", minimum=Decimal("1")),
        ShoppingCriterion(key="unparseable", minimum=Decimal("1")),
        ShoppingCriterion(key="unsupported", minimum=Decimal("1")),
    )
    request = build_request(*criteria)
    product = build_product()
    facts = (
        build_fact(
            request.id,
            product.id,
            "conflict",
            Decimal("1"),
            Decimal("2"),
            status=SpecificationNormalizationStatus.CONFLICT,
        ),
        build_fact(request.id, product.id, "multi", Decimal("1")),
        build_fact(request.id, product.id, "multi", Decimal("2")),
        build_fact(
            request.id,
            product.id,
            "unparseable",
            Decimal("1"),
            status=SpecificationNormalizationStatus.UNPARSEABLE_VALUE,
        ),
        build_fact(
            request.id,
            product.id,
            "unsupported",
            status=SpecificationNormalizationStatus.UNSUPPORTED_KEY,
        ),
    )

    evaluations = CriterionEvaluator().evaluate(criteria, facts)

    assert [item.status for item in evaluations] == [
        CriterionEvaluationStatus.MISSING,
        CriterionEvaluationStatus.CONFLICT,
        CriterionEvaluationStatus.CONFLICT,
        CriterionEvaluationStatus.UNRESOLVED,
        CriterionEvaluationStatus.UNRESOLVED,
    ]
    assert all(item.score == 0 for item in evaluations)
    assert evaluations[0].reason_code == "normalized_fact_missing"
    assert evaluations[1].observed_values == (Decimal("1"), Decimal("2"))


def test_evaluator_does_not_compare_absent_or_mismatched_units() -> None:
    criteria = (
        ShoppingCriterion(key="battery_capacity", minimum=Decimal("1")),
        ShoppingCriterion(key="weight", minimum=Decimal("1"), unit="g"),
    )
    request = build_request(*criteria)
    product = build_product()

    results = CriterionEvaluator().evaluate(
        criteria,
        (
            build_fact(request.id, product.id, "battery_capacity", Decimal("5000")),
            build_fact(request.id, product.id, "weight", Decimal("180")),
        ),
    )

    assert all(item.status is CriterionEvaluationStatus.UNRESOLVED for item in results)
    assert all(item.reason_code == "normalized_unit_mismatch" for item in results)
    assert all(item.score == 0 for item in results)


@pytest.mark.parametrize(
    ("criterion", "code"),
    (
        (ShoppingCriterion(key="weight", minimum="light"), "non_numeric_bound"),
        (ShoppingCriterion(key="weight", minimum=Decimal("-1")), "negative_bound"),
        (
            ShoppingCriterion(key="weight", minimum=Decimal("2"), maximum=Decimal("1")),
            "bounds_reversed",
        ),
        (
            ShoppingCriterion(
                key="weight",
                minimum=Decimal("2"),
                preferred=Decimal("1"),
                maximum=Decimal("3"),
            ),
            "preferred_outside_bounds",
        ),
        (
            ShoppingCriterion(
                key="weight",
                minimum=Decimal("1"),
                preferred=Decimal("4"),
                maximum=Decimal("3"),
            ),
            "preferred_outside_bounds",
        ),
        (ShoppingCriterion(key="weight", preferred=Decimal("2")), "preferred_direction_undefined"),
        (ShoppingCriterion(key="weight"), "bounds_missing"),
    ),
)
def test_evaluator_rejects_ambiguous_numeric_criterion_definitions(
    criterion: ShoppingCriterion, code: str
) -> None:
    with pytest.raises(InvalidCriterionDefinitionError) as raised:
        CriterionEvaluator().evaluate((criterion,), ())

    assert raised.value.key == "weight"
    assert raised.value.code == code


def test_evaluator_rejects_keys_that_collide_after_normalization() -> None:
    criteria = (
        ShoppingCriterion(key="battery_capacity", minimum=Decimal("1")),
        ShoppingCriterion(key="  BATTERY   CAPACITY ", minimum=Decimal("1")),
    )

    with pytest.raises(InvalidCriterionDefinitionError) as raised:
        CriterionEvaluator().evaluate(criteria, ())

    assert raised.value.key == "*"
    assert raised.value.code == "normalized_key_duplicate"


@pytest.mark.parametrize(
    ("store_type", "price_kind", "expected_risk"),
    (
        (StoreType.PLATFORM_SELF_OPERATED, PriceKind.ESTIMATED_TOTAL, "0"),
        (StoreType.BRAND_FLAGSHIP, PriceKind.UNCONDITIONAL, "0.02"),
        (StoreType.AUTHORIZED_RETAILER, PriceKind.DISPLAYED, "0.06"),
        (StoreType.THIRD_PARTY, PriceKind.CONDITIONAL, "0.14"),
        (StoreType.UNKNOWN, PriceKind.LIST, "0.18"),
    ),
)
def test_offer_cost_uses_price_fallback_and_documented_base_risk(
    store_type: StoreType, price_kind: PriceKind, expected_risk: str
) -> None:
    offer = build_offer(uuid4(), store_type=store_type, price_kind=price_kind)

    result = OfferCostEstimator().assess(offer, expected_currency="CNY")

    assert result.selected_price_kind is price_kind
    assert result.risk_coefficient == Decimal(expected_risk)
    assert result.effective_cost.amount == Decimal("100") * (Decimal("1") + result.risk_coefficient)
    assert result.comparable is True


def test_offer_cost_marks_unknown_context_and_unavailable_offers() -> None:
    product_id = uuid4()
    unknown = OfferCostEstimator().assess(
        build_offer(product_id, in_stock=None, region=None), expected_currency="CNY"
    )
    unavailable = OfferCostEstimator().assess(
        build_offer(
            product_id,
            price_kind=PriceKind.LIST,
            store_type=StoreType.UNKNOWN,
            in_stock=False,
            region=None,
        ),
        expected_currency="CNY",
    )

    assert unknown.risk_coefficient == Decimal("0.07")
    assert unknown.comparable is True
    assert unknown.reason_codes[-2:] == ("stock:unknown", "region:missing")
    assert unavailable.risk_coefficient == Decimal("0.30")
    assert unavailable.comparable is False
    assert "stock:unavailable" in unavailable.reason_codes


def test_offer_cost_excludes_missing_conditional_terms_and_currency_mismatch() -> None:
    product_id = uuid4()
    missing_terms = OfferCostEstimator().assess(
        build_offer(
            product_id,
            price_kind=PriceKind.CONDITIONAL,
            promotion_conditions=(),
        ),
        expected_currency="CNY",
    )
    mismatch = OfferCostEstimator().assess(
        build_offer(product_id, currency="USD"), expected_currency="CNY"
    )

    assert missing_terms.risk_coefficient == Decimal("0.13")
    assert missing_terms.comparable is False
    assert "conditional_terms:missing" in missing_terms.reason_codes
    assert mismatch.comparable is False
    assert mismatch.reason_codes[-1] == "currency:mismatch"


def test_builder_combines_weighted_utility_and_selects_lowest_effective_cost() -> None:
    product = build_product()
    other_product = build_product()
    request = build_request(
        ShoppingCriterion(
            key="battery_capacity",
            weight=Decimal("2"),
            hard_requirement=True,
            minimum=Decimal("5000"),
            preferred=Decimal("6000"),
            unit="unit",
        ),
        ShoppingCriterion(
            key="weight",
            weight=Decimal("1"),
            preferred=Decimal("180"),
            maximum=Decimal("200"),
            unit="unit",
        ),
    )
    ignored_request = build_request(request_id=uuid4())
    ignored_fact = build_fact(ignored_request.id, product.id, "battery_capacity", Decimal("1000"))
    facts = (
        build_fact(request.id, product.id, "battery_capacity", Decimal("5500")),
        build_fact(request.id, product.id, "weight", Decimal("190")),
        ignored_fact,
        build_fact(request.id, other_product.id, "weight", Decimal("250")),
    )
    expensive_id = UUID("00000000-0000-0000-0000-000000000001")
    cheaper_id = UUID("00000000-0000-0000-0000-000000000002")
    offers = (
        build_offer(product.id, offer_id=expensive_id, amount="100"),
        build_offer(
            product.id,
            offer_id=cheaper_id,
            amount="92",
            store_type=StoreType.BRAND_FLAGSHIP,
        ),
        build_offer(other_product.id, amount="1"),
    )

    result = CandidateScoringFoundationBuilder().build(request, product, facts, offers)

    assert result.utility == Decimal("0.750000")
    assert result.hard_requirements_met is True
    assert len(result.offer_costs) == 2
    assert result.best_offer_id == cheaper_id


def test_builder_hard_gate_and_empty_inputs_remain_explicit() -> None:
    product = build_product()
    hard_request = build_request(
        ShoppingCriterion(
            key="battery_capacity",
            hard_requirement=True,
            minimum=Decimal("5000"),
            unit="unit",
        )
    )
    failed = CandidateScoringFoundationBuilder().build(
        hard_request,
        product,
        (build_fact(hard_request.id, product.id, "battery_capacity", Decimal("4000")),),
        (),
    )
    empty_request = build_request()
    unavailable = build_offer(product.id, in_stock=False)
    empty = CandidateScoringFoundationBuilder().build(empty_request, product, (), (unavailable,))

    assert failed.hard_requirements_met is False
    assert failed.best_offer_id is None
    assert empty.utility is None
    assert empty.hard_requirements_met is True
    assert empty.best_offer_id is None


def test_builder_breaks_equal_cost_ties_by_offer_identifier() -> None:
    product = build_product()
    request = build_request()
    lower_id = UUID("00000000-0000-0000-0000-000000000001")
    higher_id = UUID("00000000-0000-0000-0000-000000000002")

    result = CandidateScoringFoundationBuilder().build(
        request,
        product,
        (),
        (
            build_offer(product.id, offer_id=higher_id),
            build_offer(product.id, offer_id=lower_id),
        ),
    )

    assert result.best_offer_id == lower_id


def test_criterion_evaluation_rejects_inconsistent_shapes() -> None:
    invalid = (
        ({"values": (Decimal("1"), Decimal("1"))}, "must be unique"),
        ({"values": (), "status": CriterionEvaluationStatus.SATISFIED}, "exactly one"),
        (
            {"values": (Decimal("1"),), "status": CriterionEvaluationStatus.CONFLICT},
            "multiple values",
        ),
        (
            {"values": (Decimal("1"),), "status": CriterionEvaluationStatus.MISSING},
            "cannot contain",
        ),
        (
            {"values": (), "status": CriterionEvaluationStatus.UNRESOLVED, "score": "0.1"},
            "zero score",
        ),
    )
    for updates, message in invalid:
        payload = {
            "key": "weight",
            "weight": Decimal("1"),
            "hard_requirement": False,
            "observed_values": updates.get("values", (Decimal("1"),)),
            "status": updates.get("status", CriterionEvaluationStatus.SATISFIED),
            "score": Decimal(str(updates.get("score", "1"))),
            "reason_code": "test",
        }
        with pytest.raises(ValidationError, match=message):
            CriterionEvaluation.model_validate(payload)


def test_offer_cost_assessment_rejects_inconsistent_formula() -> None:
    product_id = uuid4()
    base = build_cost(product_id).model_dump()
    invalid = (
        ({"reason_codes": ("x", "x")}, "must be unique"),
        ({"effective_cost": Money(amount=Decimal("100"), currency="USD")}, "currencies"),
        ({"effective_cost": Money(amount=Decimal("99"))}, "must equal"),
    )
    for updates, message in invalid:
        with pytest.raises(ValidationError, match=message):
            OfferCostAssessment.model_validate(base | updates)


def test_candidate_foundation_rejects_inconsistent_aggregates() -> None:
    product_id = uuid4()
    evaluation = build_evaluation()
    other_key = build_evaluation(key="BATTERY CAPACITY")
    cost = build_cost(product_id)
    other_product_cost = build_cost(uuid4())
    invalid = (
        (
            {
                "criterion_evaluations": (evaluation, other_key),
                "utility": Decimal("1"),
                "hard_requirements_met": True,
                "offer_costs": (),
            },
            "keys must be unique",
        ),
        (
            {
                "criterion_evaluations": (evaluation,),
                "utility": None,
                "hard_requirements_met": True,
                "offer_costs": (),
            },
            "utility exists",
        ),
        (
            {
                "criterion_evaluations": (),
                "utility": Decimal("1"),
                "hard_requirements_met": True,
                "offer_costs": (),
            },
            "utility exists",
        ),
        (
            {
                "criterion_evaluations": (
                    build_evaluation(
                        hard_requirement=True,
                        status=CriterionEvaluationStatus.UNSATISFIED,
                        score="0.5",
                    ),
                ),
                "utility": Decimal("0.5"),
                "hard_requirements_met": True,
                "offer_costs": (),
            },
            "hard requirement aggregate",
        ),
        (
            {
                "criterion_evaluations": (),
                "utility": None,
                "hard_requirements_met": True,
                "offer_costs": (cost, cost),
            },
            "identifiers must be unique",
        ),
        (
            {
                "criterion_evaluations": (),
                "utility": None,
                "hard_requirements_met": True,
                "offer_costs": (other_product_cost,),
            },
            "candidate product",
        ),
        (
            {
                "criterion_evaluations": (),
                "utility": None,
                "hard_requirements_met": True,
                "offer_costs": (cost,),
                "best_offer_id": None,
            },
            "lowest-cost comparable",
        ),
    )
    for fields, message in invalid:
        with pytest.raises(ValidationError, match=message):
            CandidateScoringFoundation.model_validate(
                {"request_id": uuid4(), "product_id": product_id} | fields
            )
