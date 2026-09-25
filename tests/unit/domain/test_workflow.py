"""Unit tests for deterministic workflow transitions and invariants."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from personal_shopping_agent.automation import (
    next_workflow_state,
)
from personal_shopping_agent.domain import (
    InvalidWorkflowTransitionError,
    ShoppingWorkflow,
    WorkflowState,
    WorkflowStateMachine,
)
from personal_shopping_agent.domain.workflow import workflow_now

OCCURRED_AT = datetime(2026, 8, 9, 11, 0, tzinfo=UTC)


def test_initialize_records_received_and_validated_events() -> None:
    machine = WorkflowStateMachine()
    request_id = uuid4()

    workflow, events = machine.initialize(request_id, occurred_at=OCCURRED_AT)

    assert workflow.request_id == request_id
    assert workflow.state is WorkflowState.REQUEST_VALIDATED
    assert workflow.revision == 1
    assert [event.sequence for event in events] == [0, 1]
    assert [event.to_state for event in events] == [
        WorkflowState.REQUEST_RECEIVED,
        WorkflowState.REQUEST_VALIDATED,
    ]
    assert events[0].from_state is None
    assert next_workflow_state(workflow) is WorkflowState.CANDIDATES_DISCOVERED


def test_state_machine_advances_every_stage_in_one_declared_order() -> None:
    machine = WorkflowStateMachine()
    workflow, _events = machine.initialize(uuid4(), occurred_at=OCCURRED_AT)
    targets = (
        WorkflowState.CANDIDATES_DISCOVERED,
        WorkflowState.OFFERS_COLLECTED,
        WorkflowState.EVIDENCE_CROSS_CHECKED,
        WorkflowState.DATA_NORMALIZED,
        WorkflowState.CANDIDATES_SCORED,
        WorkflowState.REPORT_RENDERED,
        WorkflowState.COMPLETED,
    )

    for expected_revision, target in enumerate(targets, start=2):
        previous = workflow
        workflow, event = machine.advance(
            workflow,
            target,
            reason=f"completed:{target.value}",
            occurred_at=OCCURRED_AT,
        )
        assert workflow.revision == expected_revision
        assert event.sequence == expected_revision
        assert event.from_state is previous.state
        assert event.to_state is target

    assert workflow.state is WorkflowState.COMPLETED
    assert next_workflow_state(workflow) is None
    with pytest.raises(InvalidWorkflowTransitionError, match="cannot transition"):
        machine.advance(
            workflow,
            WorkflowState.REQUEST_VALIDATED,
            reason="illegal restart",
            occurred_at=OCCURRED_AT,
        )


def test_state_machine_rejects_skips_and_records_sanitized_failure() -> None:
    machine = WorkflowStateMachine()
    workflow, _events = machine.initialize(uuid4(), occurred_at=OCCURRED_AT)

    with pytest.raises(InvalidWorkflowTransitionError, match="cannot transition"):
        machine.advance(
            workflow,
            WorkflowState.OFFERS_COLLECTED,
            reason="skip discovery",
            occurred_at=OCCURRED_AT,
        )

    failed, event = machine.fail(
        workflow,
        error_code="platform_unavailable",
        error_message="The required source is temporarily unavailable.",
        occurred_at=OCCURRED_AT,
    )

    assert failed.state is WorkflowState.FAILED
    assert failed.error_code == "platform_unavailable"
    assert event.to_state is WorkflowState.FAILED
    assert event.reason == "failed:platform_unavailable"
    with pytest.raises(InvalidWorkflowTransitionError, match="cannot fail terminal"):
        machine.fail(
            failed,
            error_code="again",
            error_message="Already terminal.",
            occurred_at=OCCURRED_AT,
        )


def test_workflow_failure_fields_are_state_dependent() -> None:
    common = {
        "request_id": uuid4(),
        "revision": 1,
        "created_at": OCCURRED_AT,
        "updated_at": OCCURRED_AT,
    }
    with pytest.raises(ValidationError, match="require an error code"):
        ShoppingWorkflow.model_validate({**common, "state": WorkflowState.FAILED})
    with pytest.raises(ValidationError, match="cannot contain failure"):
        ShoppingWorkflow.model_validate(
            {
                **common,
                "state": WorkflowState.REQUEST_VALIDATED,
                "error_code": "unexpected",
                "error_message": "unexpected",
            }
        )


def test_state_machine_default_clock_paths_return_aware_times() -> None:
    machine = WorkflowStateMachine()
    workflow, _events = machine.initialize(uuid4())
    advanced, _event = machine.advance(
        workflow,
        WorkflowState.CANDIDATES_DISCOVERED,
        reason="discovered",
    )
    failed, _failure = machine.fail(
        advanced,
        error_code="test_failure",
        error_message="Sanitized test failure.",
    )

    assert workflow_now().tzinfo is not None
    assert workflow.created_at.tzinfo is not None
    assert advanced.updated_at.tzinfo is not None
    assert failed.updated_at.tzinfo is not None


def test_state_machine_validates_reason_and_failure_details() -> None:
    machine = WorkflowStateMachine()
    workflow, _events = machine.initialize(uuid4(), occurred_at=OCCURRED_AT)

    with pytest.raises(ValidationError):
        machine.advance(
            workflow,
            WorkflowState.CANDIDATES_DISCOVERED,
            reason="",
            occurred_at=OCCURRED_AT,
        )
    with pytest.raises(ValidationError, match="require an error code"):
        machine.fail(
            workflow,
            error_code="",
            error_message="",
            occurred_at=OCCURRED_AT,
        )
