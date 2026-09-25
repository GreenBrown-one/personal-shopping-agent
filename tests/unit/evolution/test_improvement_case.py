"""Improvement cases are derived only from codes, enums, counts, and catalog keys."""

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from personal_shopping_agent.domain import (
    Budget,
    Money,
    ShoppingCriterion,
    ShoppingRequest,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
)
from personal_shopping_agent.evolution import (
    CASE_FORMAT,
    ImprovementCase,
    ImprovementCaseBuilder,
    ImprovementOutcome,
    InvalidImprovementCodeError,
    RequestShape,
)

PRIVATE_QUERY = "给住在昆明市五华区的妈妈买续航久的手机"
PRIVATE_REGION = "云南省昆明市五华区某街道"


def build_snapshot(*advance_to: WorkflowState, fail_code: str | None = None) -> WorkflowSnapshot:
    request = ShoppingRequest(
        query=PRIVATE_QUERY,
        category="老人手机",
        budget=Budget(
            maximum=Money(amount=Decimal("1999")),
            stretch_maximum=Money(amount=Decimal("2299")),
        ),
        region=PRIVATE_REGION,
        criteria=(
            ShoppingCriterion(
                key="Battery Capacity",
                minimum=Decimal("5000"),
                unit="mAh",
                hard_requirement=True,
            ),
            ShoppingCriterion(key="妈妈喜欢的颜色", preferred="红色"),
        ),
    )
    machine = WorkflowStateMachine()
    workflow, initial_events = machine.initialize(request.id)
    events = list(initial_events)
    for target in advance_to:
        workflow, event = machine.advance(workflow, target, reason="fixture")
        events.append(event)
    if fail_code is not None:
        workflow, event = machine.fail(
            workflow,
            error_code=fail_code,
            error_message=f"Raw detail mentioning {PRIVATE_REGION}.",
        )
        events.append(event)
    return WorkflowSnapshot(request=request, workflow=workflow, events=tuple(events))


def test_stalled_workflow_names_the_next_stage_and_keeps_a_reported_code() -> None:
    snapshot = build_snapshot(WorkflowState.CANDIDATES_DISCOVERED)

    case = ImprovementCaseBuilder(software_version="9.9.9").build(
        snapshot, reported_error_code="platform_access_restricted"
    )

    assert case.format == CASE_FORMAT
    assert case.software_version == "9.9.9"
    assert case.outcome is ImprovementOutcome.STALLED
    assert case.workflow_state is WorkflowState.CANDIDATES_DISCOVERED
    assert case.stage is WorkflowState.OFFERS_COLLECTED
    assert case.stable_error_code == "platform_access_restricted"
    assert case.stage_trail == (
        WorkflowState.REQUEST_RECEIVED,
        WorkflowState.REQUEST_VALIDATED,
        WorkflowState.CANDIDATES_DISCOVERED,
    )
    assert case.actual_behavior == (
        "The workflow stopped before offers_collected with error code platform_access_restricted."
    )
    assert case.request_shape == RequestShape(
        criteria_count=2,
        hard_criteria_count=1,
        supported_criterion_keys=("battery_capacity",),
        unsupported_criteria_count=1,
        region_specified=True,
        stretch_budget_specified=True,
        budget_currency="CNY",
    )


def test_failed_workflow_uses_its_own_code_and_hides_the_raw_message() -> None:
    snapshot = build_snapshot(WorkflowState.CANDIDATES_DISCOVERED, fail_code="jd_detail_parse")

    case = ImprovementCaseBuilder().build(snapshot, reported_error_code="something_else")

    assert case.outcome is ImprovementOutcome.FAILED
    assert case.stage is WorkflowState.OFFERS_COLLECTED
    assert case.stable_error_code == "jd_detail_parse"
    assert case.actual_behavior == (
        "The workflow failed while attempting offers_collected with error code jd_detail_parse."
    )
    assert case.expected_behavior.startswith("The offers_collected stage succeeds")


def test_free_text_failure_codes_are_replaced_instead_of_shared() -> None:
    snapshot = build_snapshot(fail_code=f"Failed near {PRIVATE_REGION}")

    case = ImprovementCaseBuilder().build(snapshot)

    assert case.stage is WorkflowState.CANDIDATES_DISCOVERED
    assert case.stable_error_code == "unrecognized_error_code"


def test_completed_workflow_has_no_unfinished_stage() -> None:
    snapshot = build_snapshot(
        WorkflowState.CANDIDATES_DISCOVERED,
        WorkflowState.OFFERS_COLLECTED,
        WorkflowState.EVIDENCE_CROSS_CHECKED,
        WorkflowState.DATA_NORMALIZED,
        WorkflowState.CANDIDATES_SCORED,
        WorkflowState.REPORT_RENDERED,
        WorkflowState.COMPLETED,
    )

    case = ImprovementCaseBuilder().build(snapshot)

    assert case.outcome is ImprovementOutcome.COMPLETED
    assert case.stage is None
    assert case.stable_error_code is None
    assert (
        case.actual_behavior == "The workflow completed but the user reports an unhelpful result."
    )


def test_serialized_case_never_contains_the_users_own_words() -> None:
    snapshot = build_snapshot(fail_code="fixture_failure")
    serialized = json.dumps(
        ImprovementCaseBuilder().build(snapshot).model_dump(mode="json"), ensure_ascii=False
    )

    for private in (PRIVATE_QUERY, PRIVATE_REGION, "老人手机", "妈妈", "红色", "1999", "2299"):
        assert private not in serialized
    assert str(snapshot.request.id) not in serialized
    assert str(snapshot.workflow.id) not in serialized


@pytest.mark.parametrize("code", ["Has Spaces", "https://jd.com", "", "x" * 121, "中文"])
def test_reported_codes_must_be_stable_codes(code: str) -> None:
    with pytest.raises(InvalidImprovementCodeError):
        ImprovementCaseBuilder().build(build_snapshot(), reported_error_code=code)


def _case_payload() -> dict[str, Any]:
    return ImprovementCaseBuilder().build(build_snapshot()).model_dump(mode="python")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"stage_trail": (WorkflowState.REQUEST_RECEIVED,)}, "end at the current"),
        ({"stage": None}, "only completed workflows"),
        ({"actual_behavior": f"Stopped near {PRIVATE_REGION}."}, "derived from the case"),
        ({"acceptance_checks": ("upload the database",)}, "fixed project gates"),
        ({"privacy_notice": "Anything goes."}, "privacy notice"),
        ({"fixture_ids": ("Private Fixture",)}, "String should match pattern"),
    ],
)
def test_case_contract_rejects_tampered_fields(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ImprovementCase.model_validate(_case_payload() | changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"supported_criterion_keys": ("my_secret_key",)}, "measurement catalog"),
        ({"unsupported_criteria_count": 5}, "add up"),
        ({"hard_criteria_count": 3}, "cannot exceed"),
    ],
)
def test_request_shape_rejects_free_text_keys_and_bad_counts(
    changes: dict[str, Any], message: str
) -> None:
    payload = _case_payload()["request_shape"] | changes

    with pytest.raises(ValidationError, match=message):
        RequestShape.model_validate(payload)
