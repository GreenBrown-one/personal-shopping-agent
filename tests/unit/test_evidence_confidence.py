"""Unit tests for deterministic pre-ranking evidence confidence."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import HttpUrl, ValidationError

from personal_shopping_agent.application import (
    CandidateEvidenceConfidence,
    CandidateScoringFoundation,
    CandidateScoringFoundationBuilder,
    CriterionEvidenceAssessment,
    EvidenceCheck,
    EvidenceCheckStatus,
    EvidenceConfidenceEvaluator,
    EvidenceScopeMismatchError,
    InvalidEvidenceTimelineError,
    NormalizedSpecification,
    SpecificationNormalizationStatus,
)
from personal_shopping_agent.domain import (
    Budget,
    Evidence,
    EvidenceSourceType,
    EvidenceSubjectType,
    Money,
    Product,
    ShoppingCriterion,
    ShoppingRequest,
)

NOW = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)


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
        query="续航好且重量适中的手机",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        criteria=criteria,
        created_at=NOW,
    )


def build_evidence(
    request_id: UUID,
    product_id: UUID,
    *,
    evidence_id: UUID | None = None,
    field_path: str = "specifications.battery_capacity",
    source_type: EvidenceSourceType = EvidenceSourceType.PLATFORM_LISTING,
    source_number: int = 1,
    captured_at: datetime = NOW,
    reliability: Decimal | None = None,
    freshness: Decimal | None = None,
    subject_type: EvidenceSubjectType = EvidenceSubjectType.PRODUCT,
) -> Evidence:
    return Evidence(
        id=evidence_id or uuid4(),
        request_id=request_id,
        subject_type=subject_type,
        subject_id=product_id,
        field_path=field_path,
        source_type=source_type,
        source_url=HttpUrl(f"https://source{source_number}.example.com/specifications"),
        source_title="Example specifications",
        captured_at=captured_at,
        observed_value="5000 mAh",
        reliability=reliability,
        freshness=freshness,
    )


def build_specification(
    request_id: UUID,
    product_id: UUID,
    key: str,
    evidence_ids: tuple[UUID, ...],
    *,
    values: tuple[Decimal, ...] = (Decimal("5000"),),
    unit: str = "mAh",
    status: SpecificationNormalizationStatus = SpecificationNormalizationStatus.NORMALIZED,
    normalized_at: datetime = NOW,
    source_field_paths: tuple[str, ...] | None = None,
) -> NormalizedSpecification:
    return NormalizedSpecification(
        request_id=request_id,
        workflow_id=uuid4(),
        product_id=product_id,
        canonical_key=key,
        source_field_paths=source_field_paths or (f"specifications.{key}",),
        raw_values=("raw",),
        normalized_values=values,
        canonical_unit=unit,
        evidence_ids=evidence_ids,
        status=status,
        normalized_at=normalized_at,
    )


def build_check(
    request_id: UUID,
    product_id: UUID,
    status: EvidenceCheckStatus,
    *,
    field_path: str = "specifications.battery_capacity",
    checked_at: datetime = NOW,
) -> EvidenceCheck:
    return EvidenceCheck(
        request_id=request_id,
        workflow_id=uuid4(),
        product_id=product_id,
        field_path=field_path,
        status=status,
        platform_values=("5000 mAh",),
        checked_at=checked_at,
    )


def build_foundation(
    request: ShoppingRequest,
    product: Product,
    specifications: tuple[NormalizedSpecification, ...],
) -> CandidateScoringFoundation:
    return CandidateScoringFoundationBuilder().build(request, product, specifications, ())


def battery_criterion(*, weight: str = "1") -> ShoppingCriterion:
    return ShoppingCriterion(
        key="battery_capacity",
        weight=Decimal(weight),
        minimum=Decimal("4000"),
        preferred=Decimal("6000"),
        unit="mAh",
    )


def weight_criterion(*, weight: str = "1") -> ShoppingCriterion:
    return ShoppingCriterion(
        key="weight",
        weight=Decimal(weight),
        preferred=Decimal("160"),
        maximum=Decimal("200"),
        unit="g",
    )


def assess(
    request: ShoppingRequest,
    product: Product,
    specifications: tuple[NormalizedSpecification, ...],
    evidence: tuple[Evidence, ...],
    checks: tuple[EvidenceCheck, ...] = (),
    *,
    assessed_at: datetime = NOW,
    foundation: CandidateScoringFoundation | None = None,
) -> CandidateEvidenceConfidence:
    return EvidenceConfidenceEvaluator().assess(
        request=request,
        foundation=foundation or build_foundation(request, product, specifications),
        specifications=specifications,
        evidence=evidence,
        checks=checks,
        assessed_at=assessed_at,
    )


def test_evaluator_combines_weighted_components_and_adjusts_utility() -> None:
    product = build_product()
    request = build_request(battery_criterion(weight="2"), weight_criterion())
    platform_battery = build_evidence(
        request.id,
        product.id,
        source_type=EvidenceSourceType.PLATFORM_LISTING,
        source_number=1,
        captured_at=NOW - timedelta(days=20),
    )
    official_battery = build_evidence(
        request.id,
        product.id,
        source_type=EvidenceSourceType.MANUFACTURER_OFFICIAL,
        source_number=2,
        captured_at=NOW - timedelta(days=400),
    )
    weight = build_evidence(
        request.id,
        product.id,
        field_path="specifications.weight",
        source_type=EvidenceSourceType.PLATFORM_SELF_OPERATED,
        source_number=3,
        captured_at=NOW - timedelta(days=200),
    )
    specifications = (
        build_specification(
            request.id,
            product.id,
            "battery_capacity",
            (platform_battery.id, official_battery.id),
            values=(Decimal("5000"),),
        ),
        build_specification(
            request.id,
            product.id,
            "weight",
            (weight.id,),
            values=(Decimal("180"),),
            unit="g",
        ),
    )
    check = build_check(
        request.id,
        product.id,
        EvidenceCheckStatus.MATCH,
        field_path="specifications.weight",
    )

    result = assess(
        request,
        product,
        specifications,
        (platform_battery, official_battery, weight),
        (check,),
    )

    assert result.completeness == Decimal("1.000000")
    assert result.source_reliability == Decimal("0.933333")
    assert result.freshness == Decimal("0.966667")
    assert result.consistency == Decimal("1.000000")
    assert result.evidence_confidence == Decimal("0.902222")
    assert result.base_utility == Decimal("0.750000")
    assert result.adjusted_utility == Decimal("0.676667")
    assert result.criteria[0].reason_codes[-1] == ("consistency:normalized_multi_source_agreement")
    assert result.criteria[1].reason_codes[-1] == "consistency:official_match"


def test_completeness_uses_all_weights_but_quality_uses_only_complete_criteria() -> None:
    product = build_product()
    request = build_request(battery_criterion(weight="2"), weight_criterion())
    official = build_evidence(
        request.id,
        product.id,
        source_type=EvidenceSourceType.MANUFACTURER_OFFICIAL,
    )
    specification = build_specification(request.id, product.id, "battery_capacity", (official.id,))

    result = assess(request, product, (specification,), (official,))

    assert result.completeness == Decimal("0.666667")
    assert result.source_reliability == Decimal("0.950000")
    assert result.freshness == Decimal("1.000000")
    assert result.consistency == Decimal("0.500000")
    assert result.evidence_confidence == Decimal("0.316667")
    assert result.criteria[1].complete is False
    assert result.criteria[1].confidence == 0


@pytest.mark.parametrize(
    ("source_type", "expected"),
    (
        (EvidenceSourceType.MANUFACTURER_OFFICIAL, "0.95"),
        (EvidenceSourceType.PLATFORM_SELF_OPERATED, "0.90"),
        (EvidenceSourceType.BRAND_FLAGSHIP, "0.85"),
        (EvidenceSourceType.AUTHORIZED_RETAILER, "0.80"),
        (EvidenceSourceType.INDEPENDENT_REVIEW, "0.75"),
        (EvidenceSourceType.THIRD_PARTY_SELLER, "0.65"),
        (EvidenceSourceType.PLATFORM_LISTING, "0.60"),
        (EvidenceSourceType.USER_COMMENT, "0.45"),
        (EvidenceSourceType.SEARCH_SNIPPET, "0.30"),
    ),
)
def test_source_type_prior_is_used_only_when_no_explicit_reliability_exists(
    source_type: EvidenceSourceType, expected: str
) -> None:
    product = build_product()
    request = build_request(battery_criterion())
    item = build_evidence(request.id, product.id, source_type=source_type)
    specification = build_specification(request.id, product.id, "battery_capacity", (item.id,))

    result = assess(request, product, (specification,), (item,))

    assert result.source_reliability == Decimal(expected)


def test_explicit_reliability_and_freshness_override_default_priors() -> None:
    product = build_product()
    request = build_request(battery_criterion())
    item = build_evidence(
        request.id,
        product.id,
        source_type=EvidenceSourceType.SEARCH_SNIPPET,
        captured_at=NOW - timedelta(days=900),
        reliability=Decimal("0.42"),
        freshness=Decimal("0.33"),
    )
    specification = build_specification(request.id, product.id, "battery_capacity", (item.id,))

    result = assess(request, product, (specification,), (item,))

    assert result.source_reliability == Decimal("0.420000")
    assert result.freshness == Decimal("0.330000")


@pytest.mark.parametrize(
    ("age_days", "expected"),
    ((180, "1"), (181, "0.90"), (365, "0.90"), (366, "0.80"), (730, "0.80"), (731, "0.70")),
)
def test_default_specification_freshness_uses_documented_age_bands(
    age_days: int, expected: str
) -> None:
    product = build_product()
    request = build_request(battery_criterion())
    item = build_evidence(request.id, product.id, captured_at=NOW - timedelta(days=age_days))
    specification = build_specification(request.id, product.id, "battery_capacity", (item.id,))

    result = assess(request, product, (specification,), (item,))

    assert result.freshness == Decimal(expected)


@pytest.mark.parametrize(
    ("status", "expected", "reason"),
    (
        (EvidenceCheckStatus.MATCH, "1", "consistency:official_match"),
        (EvidenceCheckStatus.CONFLICT, "0", "consistency:official_conflict"),
        (EvidenceCheckStatus.PLATFORM_ONLY, "0.60", "consistency:platform_only"),
        (EvidenceCheckStatus.OFFICIAL_ONLY, "0.75", "consistency:official_only"),
        (
            EvidenceCheckStatus.MISSING_OFFICIAL_SOURCE,
            "0.50",
            "consistency:missing_official_source",
        ),
    ),
)
def test_single_source_consistency_uses_relevant_cross_check_status(
    status: EvidenceCheckStatus, expected: str, reason: str
) -> None:
    product = build_product()
    request = build_request(battery_criterion())
    item = build_evidence(request.id, product.id)
    specification = build_specification(request.id, product.id, "battery_capacity", (item.id,))
    check = build_check(request.id, product.id, status)

    result = assess(request, product, (specification,), (item,), (check,))

    assert result.consistency == Decimal(expected)
    assert result.criteria[0].reason_codes[-1] == reason


def test_most_conservative_unilateral_check_wins_without_match_or_conflict() -> None:
    product = build_product()
    request = build_request(battery_criterion())
    item = build_evidence(request.id, product.id)
    specification = build_specification(request.id, product.id, "battery_capacity", (item.id,))
    checks = (
        build_check(request.id, product.id, EvidenceCheckStatus.OFFICIAL_ONLY),
        build_check(request.id, product.id, EvidenceCheckStatus.PLATFORM_ONLY),
    )

    result = assess(request, product, (specification,), (item,), checks)

    assert result.consistency == Decimal("0.600000")
    assert result.criteria[0].reason_codes[-1] == "consistency:platform_only"


def test_normalized_multi_source_agreement_overrides_raw_cross_check_conflict() -> None:
    product = build_product()
    request = build_request(battery_criterion())
    first = build_evidence(request.id, product.id, source_number=1)
    second = build_evidence(
        request.id,
        product.id,
        source_type=EvidenceSourceType.MANUFACTURER_OFFICIAL,
        source_number=2,
    )
    specification = build_specification(
        request.id,
        product.id,
        "battery_capacity",
        (first.id, second.id),
        source_field_paths=(
            "specifications.battery_capacity",
            "specifications.battery",
        ),
    )
    conflict = build_check(request.id, product.id, EvidenceCheckStatus.CONFLICT)

    result = assess(request, product, (specification,), (first, second), (conflict,))

    assert result.consistency == Decimal("1.000000")
    assert result.criteria[0].reason_codes[-1] == ("consistency:normalized_multi_source_agreement")


def test_same_url_is_not_counted_as_two_independent_sources() -> None:
    product = build_product()
    request = build_request(battery_criterion())
    first = build_evidence(request.id, product.id, source_number=1)
    second = build_evidence(
        request.id,
        product.id,
        source_type=EvidenceSourceType.MANUFACTURER_OFFICIAL,
        source_number=1,
    )
    specification = build_specification(
        request.id, product.id, "battery_capacity", (first.id, second.id)
    )

    result = assess(request, product, (specification,), (first, second))

    assert result.consistency == Decimal("0.500000")
    assert result.criteria[0].reason_codes[-1] == "consistency:single_source_unverified"


def test_missing_referenced_evidence_is_explicit_and_scores_zero_quality() -> None:
    product = build_product()
    request = build_request(battery_criterion())
    specification = build_specification(request.id, product.id, "battery_capacity", (uuid4(),))

    result = assess(request, product, (specification,), ())

    criterion = result.criteria[0]
    assert criterion.complete is True
    assert criterion.confidence == 0
    assert criterion.consistency == 0
    assert "evidence:missing_reference" in criterion.reason_codes
    assert criterion.reason_codes[-1] == "consistency:no_linked_evidence"


def test_empty_request_has_no_artificial_confidence_or_adjusted_utility() -> None:
    product = build_product()
    request = build_request()

    result = assess(request, product, (), ())

    assert result.criteria == ()
    assert result.completeness == 0
    assert result.source_reliability == 0
    assert result.freshness == 0
    assert result.consistency == 0
    assert result.evidence_confidence == 0
    assert result.base_utility is None
    assert result.adjusted_utility is None


def test_unrelated_scope_and_subject_evidence_are_ignored() -> None:
    product = build_product()
    other_product = build_product()
    request = build_request(battery_criterion())
    other_request = build_request(request_id=uuid4())
    linked = build_evidence(request.id, product.id)
    ignored = (
        build_evidence(other_request.id, product.id, evidence_id=linked.id),
        build_evidence(request.id, other_product.id),
        build_evidence(
            request.id,
            product.id,
            subject_type=EvidenceSubjectType.OFFER,
            evidence_id=uuid4(),
        ),
    )
    specification = build_specification(request.id, product.id, "battery_capacity", (linked.id,))
    unrelated_future_specification = build_specification(
        request.id,
        product.id,
        "weight",
        (uuid4(),),
        values=(Decimal("180"),),
        unit="g",
        normalized_at=NOW + timedelta(days=1),
    )
    unrelated_future_check = build_check(
        request.id,
        product.id,
        EvidenceCheckStatus.CONFLICT,
        field_path="specifications.weight",
        checked_at=NOW + timedelta(days=1),
    )

    result = assess(
        request,
        product,
        (specification, unrelated_future_specification),
        (linked, *ignored),
        (unrelated_future_check,),
        foundation=build_foundation(request, product, (specification,)),
    )

    assert result.criteria[0].evidence_ids == (linked.id,)
    assert result.consistency == Decimal("0.500000")


def test_scope_mismatches_and_duplicate_evidence_are_rejected() -> None:
    product = build_product()
    request = build_request(battery_criterion())
    item = build_evidence(request.id, product.id)
    specification = build_specification(request.id, product.id, "battery_capacity", (item.id,))
    foundation = build_foundation(request, product, (specification,))

    with pytest.raises(EvidenceScopeMismatchError) as wrong_request:
        assess(
            request,
            product,
            (specification,),
            (item,),
            foundation=foundation.model_copy(update={"request_id": uuid4()}),
        )
    assert wrong_request.value.code == "request_mismatch"

    changed_request = build_request(weight_criterion(), request_id=request.id)
    with pytest.raises(EvidenceScopeMismatchError) as wrong_criteria:
        assess(
            changed_request,
            product,
            (specification,),
            (item,),
            foundation=foundation,
        )
    assert wrong_criteria.value.code == "criteria_mismatch"

    with pytest.raises(EvidenceScopeMismatchError) as duplicate:
        assess(request, product, (specification,), (item, item), foundation=foundation)
    assert duplicate.value.code == "duplicate_evidence_id"


def test_future_evidence_and_derived_records_are_rejected() -> None:
    product = build_product()
    request = build_request(battery_criterion())
    future_evidence = build_evidence(request.id, product.id, captured_at=NOW + timedelta(seconds=1))
    specification = build_specification(
        request.id, product.id, "battery_capacity", (future_evidence.id,)
    )

    with pytest.raises(InvalidEvidenceTimelineError) as evidence_error:
        assess(request, product, (specification,), (future_evidence,))
    assert evidence_error.value.record_id == future_evidence.id
    assert evidence_error.value.code == "evidence_from_future"

    valid_evidence = build_evidence(request.id, product.id)
    future_specification = build_specification(
        request.id,
        product.id,
        "battery_capacity",
        (valid_evidence.id,),
        normalized_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(InvalidEvidenceTimelineError) as specification_error:
        assess(request, product, (future_specification,), (valid_evidence,))
    assert specification_error.value.record_id == future_specification.id
    assert specification_error.value.code == "derived_record_from_future"

    future_check = build_check(
        request.id,
        product.id,
        EvidenceCheckStatus.MATCH,
        checked_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(InvalidEvidenceTimelineError) as check_error:
        assess(
            request,
            product,
            (specification,),
            (valid_evidence,),
            (future_check,),
            foundation=build_foundation(request, product, (specification,)),
        )
    assert check_error.value.record_id == future_check.id


def test_naive_assessment_time_is_rejected() -> None:
    product = build_product()
    request = build_request()

    with pytest.raises(InvalidEvidenceTimelineError) as raised:
        assess(request, product, (), (), assessed_at=datetime(2026, 8, 9, 23, 0))

    assert raised.value.record_id is None
    assert raised.value.code == "assessment_time_naive"


def valid_criterion_assessment() -> CriterionEvidenceAssessment:
    return CriterionEvidenceAssessment(
        key="battery_capacity",
        weight=Decimal("1"),
        complete=True,
        source_reliability=Decimal("0.8"),
        freshness=Decimal("0.9"),
        consistency=Decimal("0.5"),
        confidence=Decimal("0.36"),
        evidence_ids=(uuid4(),),
        reason_codes=("test",),
    )


def test_criterion_assessment_rejects_inconsistent_shapes() -> None:
    valid = valid_criterion_assessment().model_dump()
    duplicate_id = uuid4()
    invalid = (
        ({"evidence_ids": (duplicate_id, duplicate_id)}, "identifiers must be unique"),
        ({"reason_codes": ("x", "x")}, "reason codes must be unique"),
        (
            {
                "complete": False,
                "source_reliability": Decimal("0.1"),
                "freshness": Decimal("0"),
                "consistency": Decimal("0"),
                "confidence": Decimal("0"),
            },
            "components must be zero",
        ),
        ({"confidence": Decimal("0.35")}, "component product"),
    )
    for updates, message in invalid:
        with pytest.raises(ValidationError, match=message):
            CriterionEvidenceAssessment.model_validate(valid | updates)


def valid_candidate_confidence() -> CandidateEvidenceConfidence:
    criterion = valid_criterion_assessment()
    return CandidateEvidenceConfidence(
        request_id=uuid4(),
        product_id=uuid4(),
        criteria=(criterion,),
        completeness=Decimal("1"),
        source_reliability=Decimal("0.8"),
        freshness=Decimal("0.9"),
        consistency=Decimal("0.5"),
        evidence_confidence=Decimal("0.36"),
        base_utility=Decimal("0.75"),
        adjusted_utility=Decimal("0.27"),
        assessed_at=NOW,
    )


def test_candidate_confidence_rejects_inconsistent_aggregates() -> None:
    valid = valid_candidate_confidence().model_dump()
    duplicate_key = valid_criterion_assessment().model_copy(update={"key": "BATTERY CAPACITY"})
    invalid = (
        ({"criteria": (valid_criterion_assessment(), duplicate_key)}, "keys must be unique"),
        ({"base_utility": None, "adjusted_utility": None}, "base utility exists"),
        ({"adjusted_utility": None}, "present together"),
        ({"completeness": Decimal("0.5")}, "components must match"),
        ({"evidence_confidence": Decimal("0.35")}, "component product"),
        ({"adjusted_utility": Decimal("0.26")}, "utility times"),
    )
    for updates, message in invalid:
        with pytest.raises(ValidationError, match=message):
            CandidateEvidenceConfidence.model_validate(valid | updates)


def test_empty_candidate_model_requires_no_base_or_adjusted_utility() -> None:
    empty = CandidateEvidenceConfidence(
        request_id=uuid4(),
        product_id=uuid4(),
        criteria=(),
        completeness=Decimal("0"),
        source_reliability=Decimal("0"),
        freshness=Decimal("0"),
        consistency=Decimal("0"),
        evidence_confidence=Decimal("0"),
        assessed_at=NOW,
    )

    assert empty.base_utility is None
    assert empty.adjusted_utility is None
