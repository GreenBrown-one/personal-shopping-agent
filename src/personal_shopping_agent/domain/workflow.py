"""Deterministic shopping workflow models and transition rules."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from personal_shopping_agent.domain.models import ShoppingRequest
from personal_shopping_agent.domain.serialization import JsonContractModel


class WorkflowState(StrEnum):
    """Auditable stages of one shopping decision workflow."""

    REQUEST_RECEIVED = "request_received"
    REQUEST_VALIDATED = "request_validated"
    CANDIDATES_DISCOVERED = "candidates_discovered"
    OFFERS_COLLECTED = "offers_collected"
    EVIDENCE_CROSS_CHECKED = "evidence_cross_checked"
    DATA_NORMALIZED = "data_normalized"
    CANDIDATES_SCORED = "candidates_scored"
    REPORT_RENDERED = "report_rendered"
    COMPLETED = "completed"
    FAILED = "failed"


NEXT_STATE: dict[WorkflowState, WorkflowState] = {
    WorkflowState.REQUEST_RECEIVED: WorkflowState.REQUEST_VALIDATED,
    WorkflowState.REQUEST_VALIDATED: WorkflowState.CANDIDATES_DISCOVERED,
    WorkflowState.CANDIDATES_DISCOVERED: WorkflowState.OFFERS_COLLECTED,
    WorkflowState.OFFERS_COLLECTED: WorkflowState.EVIDENCE_CROSS_CHECKED,
    WorkflowState.EVIDENCE_CROSS_CHECKED: WorkflowState.DATA_NORMALIZED,
    WorkflowState.DATA_NORMALIZED: WorkflowState.CANDIDATES_SCORED,
    WorkflowState.CANDIDATES_SCORED: WorkflowState.REPORT_RENDERED,
    WorkflowState.REPORT_RENDERED: WorkflowState.COMPLETED,
}


def workflow_now() -> datetime:
    """Return an aware UTC timestamp for workflow defaults."""

    return datetime.now(UTC)


class WorkflowModel(JsonContractModel):
    """Strict immutable base for orchestration state and audit events."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ShoppingWorkflow(WorkflowModel):
    """Current durable state of one shopping request execution."""

    id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    state: WorkflowState
    revision: int = Field(ge=0)
    created_at: AwareDatetime = Field(default_factory=workflow_now)
    updated_at: AwareDatetime = Field(default_factory=workflow_now)
    error_code: str | None = Field(default=None, max_length=120)
    error_message: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def failure_fields_match_state(self) -> Self:
        """Require sanitized failure details only for failed workflows."""

        if self.state is WorkflowState.FAILED:
            if not self.error_code or not self.error_message:
                raise ValueError("failed workflows require an error code and message")
        elif self.error_code is not None or self.error_message is not None:
            raise ValueError("non-failed workflows cannot contain failure details")
        return self


class WorkflowEvent(WorkflowModel):
    """One ordered and immutable workflow transition audit event."""

    id: UUID = Field(default_factory=uuid4)
    workflow_id: UUID
    sequence: int = Field(ge=0)
    from_state: WorkflowState | None
    to_state: WorkflowState
    occurred_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=240)


class WorkflowSnapshot(WorkflowModel):
    """Complete user-facing view of request, current state, and audit trail."""

    request: ShoppingRequest
    workflow: ShoppingWorkflow
    events: tuple[WorkflowEvent, ...]


class InvalidWorkflowTransitionError(ValueError):
    """Raised when a caller tries to skip, reverse, or leave a terminal state."""


class WorkflowStateMachine:
    """Pure transition engine with no database, MCP, browser, or LLM dependency."""

    @staticmethod
    def next_state(state: WorkflowState) -> WorkflowState | None:
        """Return the only permitted normal successor, or None for terminal states."""

        return NEXT_STATE.get(state)

    def initialize(
        self, request_id: UUID, *, occurred_at: datetime | None = None
    ) -> tuple[ShoppingWorkflow, tuple[WorkflowEvent, WorkflowEvent]]:
        """Create a validated workflow and its first two audit events."""

        timestamp = occurred_at or workflow_now()
        workflow_id = uuid4()
        workflow = ShoppingWorkflow(
            id=workflow_id,
            request_id=request_id,
            state=WorkflowState.REQUEST_VALIDATED,
            revision=1,
            created_at=timestamp,
            updated_at=timestamp,
        )
        received = WorkflowEvent(
            workflow_id=workflow_id,
            sequence=0,
            from_state=None,
            to_state=WorkflowState.REQUEST_RECEIVED,
            occurred_at=timestamp,
            reason="request_received",
        )
        validated = WorkflowEvent(
            workflow_id=workflow_id,
            sequence=1,
            from_state=WorkflowState.REQUEST_RECEIVED,
            to_state=WorkflowState.REQUEST_VALIDATED,
            occurred_at=timestamp,
            reason="request_validated",
        )
        return workflow, (received, validated)

    def advance(
        self,
        workflow: ShoppingWorkflow,
        target: WorkflowState,
        *,
        reason: str,
        occurred_at: datetime | None = None,
    ) -> tuple[ShoppingWorkflow, WorkflowEvent]:
        """Advance exactly one declared stage and emit its audit event."""

        expected = self.next_state(workflow.state)
        if expected is None or target is not expected:
            raise InvalidWorkflowTransitionError(
                f"cannot transition from {workflow.state.value} to {target.value}"
            )
        timestamp = occurred_at or workflow_now()
        revision = workflow.revision + 1
        updated = workflow.model_copy(
            update={
                "state": target,
                "revision": revision,
                "updated_at": timestamp,
                "error_code": None,
                "error_message": None,
            }
        )
        updated = ShoppingWorkflow.model_validate(updated.model_dump(mode="python"))
        event = WorkflowEvent(
            workflow_id=workflow.id,
            sequence=revision,
            from_state=workflow.state,
            to_state=target,
            occurred_at=timestamp,
            reason=reason,
        )
        return updated, event

    def fail(
        self,
        workflow: ShoppingWorkflow,
        *,
        error_code: str,
        error_message: str,
        occurred_at: datetime | None = None,
    ) -> tuple[ShoppingWorkflow, WorkflowEvent]:
        """Move a non-terminal workflow to a sanitized failed state."""

        if self.next_state(workflow.state) is None:
            raise InvalidWorkflowTransitionError(
                f"cannot fail terminal workflow in {workflow.state.value}"
            )
        timestamp = occurred_at or workflow_now()
        revision = workflow.revision + 1
        updated = workflow.model_copy(
            update={
                "state": WorkflowState.FAILED,
                "revision": revision,
                "updated_at": timestamp,
                "error_code": error_code,
                "error_message": error_message,
            }
        )
        event = WorkflowEvent(
            workflow_id=workflow.id,
            sequence=revision,
            from_state=workflow.state,
            to_state=WorkflowState.FAILED,
            occurred_at=timestamp,
            reason=f"failed:{error_code}",
        )
        return ShoppingWorkflow.model_validate(updated.model_dump(mode="python")), event
