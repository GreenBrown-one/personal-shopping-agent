"""Deterministic requirement review mirrors the scoring rules without guessing values."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from personal_shopping_agent.domain import Budget, Money, ShoppingCriterion, ShoppingRequest
from personal_shopping_agent.intake import (
    RequirementClarificationRequiredError,
    RequirementIssue,
    RequirementIssueCode,
    RequirementIssueSeverity,
    RequirementReview,
    RequirementReviewer,
)

BLOCKING = RequirementIssueSeverity.BLOCKING
WARNING = RequirementIssueSeverity.WARNING


def build_request(
    *criteria: ShoppingCriterion,
    region: str | None = "云南省曲靖市",
) -> ShoppingRequest:
    return ShoppingRequest(
        query="续航优先的手机",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("3000"))),
        region=region,
        criteria=criteria,
    )


def battery(**overrides: object) -> ShoppingCriterion:
    values: dict[str, object] = {
        "key": "battery_capacity",
        "minimum": Decimal("5000"),
        "unit": "mAh",
    }
    values.update(overrides)
    return ShoppingCriterion.model_validate(values)


def issue_codes(review: RequirementReview) -> list[tuple[RequirementIssueCode, str]]:
    return [(item.code, item.severity.value) for item in review.issues]


def test_complete_request_is_ready_to_start_and_rank() -> None:
    review = RequirementReviewer().review(
        build_request(battery(), battery(key="weight", minimum=None, maximum=200, unit="g"))
    )

    assert review.ready_to_start is True
    assert review.ready_for_ranking is True
    assert review.issues == ()
    assert review.blocking_issues == ()
    assert [item.canonical_key for item in review.supported_measurements] == [
        "battery_capacity",
        "weight",
        "display_size",
        "storage_capacity",
        "memory_capacity",
        "chip_performance",
    ]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"minimum": True}, RequirementIssueCode.NON_NUMERIC_BOUND),
        ({"minimum": "large"}, RequirementIssueCode.NON_NUMERIC_BOUND),
        ({"minimum": Decimal("-1")}, RequirementIssueCode.NEGATIVE_BOUND),
        ({"maximum": Decimal("4000")}, RequirementIssueCode.BOUNDS_REVERSED),
        ({"preferred": Decimal("4000")}, RequirementIssueCode.PREFERRED_OUTSIDE_BOUNDS),
        (
            {"minimum": None, "preferred": Decimal("5000")},
            RequirementIssueCode.PREFERRED_DIRECTION_UNDEFINED,
        ),
        ({"minimum": None}, RequirementIssueCode.BOUNDS_MISSING),
    ],
)
def test_bounds_that_would_break_scoring_are_blocking(
    overrides: dict[str, object], code: RequirementIssueCode
) -> None:
    review = RequirementReviewer().review(build_request(battery(**overrides)))

    assert issue_codes(review) == [(code, "blocking")]
    assert review.issues[0].criterion_key == "battery_capacity"
    assert review.ready_to_start is False
    assert review.ready_for_ranking is False
    assert review.blocking_issues == review.issues


def test_keys_that_collide_after_folding_are_blocking() -> None:
    review = RequirementReviewer().review(build_request(battery(), battery(key="Battery Capacity")))

    assert issue_codes(review) == [(RequirementIssueCode.CRITERION_KEY_DUPLICATE, "blocking")]
    assert review.issues[0].criterion_key == "Battery Capacity"


@pytest.mark.parametrize(("hard", "severity"), [(False, WARNING), (True, BLOCKING)])
def test_alias_keys_suggest_the_canonical_key_without_rewriting(
    hard: bool, severity: RequirementIssueSeverity
) -> None:
    alias = battery(key="内存", minimum=Decimal("8"), unit="GB", hard_requirement=hard)
    review = RequirementReviewer().review(build_request(alias))

    assert [(item.code, item.severity) for item in review.issues] == [
        (RequirementIssueCode.CRITERION_KEY_ALIAS, severity)
    ]
    assert "'memory_capacity'" in review.issues[0].suggestion
    assert "'GB'" in review.issues[0].suggestion
    assert ("exclude every candidate" in review.issues[0].message) is hard
    assert review.ready_for_ranking is False


@pytest.mark.parametrize(("hard", "severity"), [(False, WARNING), (True, BLOCKING)])
def test_unsupported_keys_list_the_supported_catalog(
    hard: bool, severity: RequirementIssueSeverity
) -> None:
    unsupported = battery(key="camera quality", unit=None, hard_requirement=hard)
    review = RequirementReviewer().review(build_request(battery(), unsupported))

    assert [(item.code, item.severity) for item in review.issues] == [
        (RequirementIssueCode.CRITERION_KEY_UNSUPPORTED, severity)
    ]
    assert "battery_capacity (mAh)" in review.issues[0].suggestion
    assert review.ready_to_start is not hard
    assert review.ready_for_ranking is not hard


def test_missing_and_mismatched_units_are_reported_with_explicit_suggestions() -> None:
    review = RequirementReviewer().review(
        build_request(
            battery(unit=None),
            battery(key="weight", minimum=None, maximum=Decimal("0.2"), unit="kg"),
            battery(key="display_size", minimum=Decimal("6"), unit="cm"),
        )
    )

    assert issue_codes(review) == [
        (RequirementIssueCode.CRITERION_UNIT_MISSING, "warning"),
        (RequirementIssueCode.CRITERION_UNIT_MISMATCH, "warning"),
        (RequirementIssueCode.CRITERION_UNIT_MISMATCH, "warning"),
    ]
    assert review.issues[0].suggestion == "Confirm the bounds in 'mAh'."
    assert review.issues[1].suggestion.startswith("Convert the bounds from 'kg' to 'g'")
    assert review.issues[2].suggestion == "Confirm the bounds in 'inch'."
    assert review.ready_to_start is True
    assert review.ready_for_ranking is False


def test_empty_criteria_and_missing_region_are_warnings_only() -> None:
    review = RequirementReviewer().review(build_request(region=None))

    assert issue_codes(review) == [
        (RequirementIssueCode.NO_CRITERIA, "warning"),
        (RequirementIssueCode.REGION_MISSING, "warning"),
    ]
    assert all(item.criterion_key is None for item in review.issues)
    assert review.ready_to_start is True
    assert review.ready_for_ranking is False


def test_require_ready_returns_the_review_or_raises_with_stable_codes() -> None:
    reviewer = RequirementReviewer()
    assert reviewer.require_ready(build_request(battery())).ready_for_ranking is True

    with pytest.raises(RequirementClarificationRequiredError) as raised:
        reviewer.require_ready(build_request(battery(minimum=None)))

    assert "battery_capacity: bounds_missing" in str(raised.value)
    assert raised.value.review.ready_to_start is False


def test_review_contract_rejects_inconsistent_readiness() -> None:
    blocking = RequirementIssue(
        code=RequirementIssueCode.BOUNDS_MISSING,
        severity=BLOCKING,
        criterion_key="battery_capacity",
        message="missing",
        suggestion="ask",
    )

    with pytest.raises(ValidationError, match="ready_to_start"):
        RequirementReview(
            ready_to_start=True,
            ready_for_ranking=False,
            issues=(blocking,),
            supported_measurements=(),
        )
    with pytest.raises(ValidationError, match="cannot be ready for ranking"):
        RequirementReview(
            ready_to_start=False,
            ready_for_ranking=True,
            issues=(blocking,),
            supported_measurements=(),
        )
