"""Application service coordinating deterministic workflow state and persistence."""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from personal_shopping_agent.domain import ShoppingRequest
from personal_shopping_agent.domain.workflow import (
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStateMachine,
    workflow_now,
)


class WorkflowRepository(Protocol):
    """Application-owned persistence port implemented by storage adapters."""

    def start(
        self,
        request: ShoppingRequest,
        workflow: ShoppingWorkflow,
        events: tuple[WorkflowEvent, ...],
    ) -> None: ...

    def get_snapshot(self, workflow_id: UUID) -> WorkflowSnapshot: ...

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None: ...


class ShoppingWorkflowService:
    """Use-case boundary shared by MCP now and future local interfaces."""

    def __init__(
        self,
        repository: WorkflowRepository,
        *,
        state_machine: WorkflowStateMachine | None = None,
        clock: Callable[[], datetime] = workflow_now,
    ) -> None:
        self._repository = repository
        self._state_machine = state_machine or WorkflowStateMachine()
        self._clock = clock

    def start(self, request: ShoppingRequest) -> WorkflowSnapshot:
        """Persist a validated request and initialize its workflow atomically."""

        workflow, events = self._state_machine.initialize(request.id, occurred_at=self._clock())
        self._repository.start(request, workflow, events)
        return WorkflowSnapshot(request=request, workflow=workflow, events=events)

    def get(self, workflow_id: UUID) -> WorkflowSnapshot:
        """Return the current durable workflow snapshot."""

        return self._repository.get_snapshot(workflow_id)

    def advance(self, workflow_id: UUID, target: WorkflowState, *, reason: str) -> WorkflowSnapshot:
        """Advance one stage after a future application step has actually succeeded."""

        snapshot = self._repository.get_snapshot(workflow_id)
        updated, event = self._state_machine.advance(
            snapshot.workflow,
            target,
            reason=reason,
            occurred_at=self._clock(),
        )
        self._repository.save_transition(
            updated,
            event,
            expected_revision=snapshot.workflow.revision,
        )
        return self._repository.get_snapshot(workflow_id)

    def fail(
        self,
        workflow_id: UUID,
        *,
        error_code: str,
        error_message: str,
    ) -> WorkflowSnapshot:
        """Record a sanitized terminal failure without exposing raw exceptions."""

        snapshot = self._repository.get_snapshot(workflow_id)
        updated, event = self._state_machine.fail(
            snapshot.workflow,
            error_code=error_code,
            error_message=error_message,
            occurred_at=self._clock(),
        )
        self._repository.save_transition(
            updated,
            event,
            expected_revision=snapshot.workflow.revision,
        )
        return self._repository.get_snapshot(workflow_id)


def next_workflow_state(workflow: ShoppingWorkflow) -> WorkflowState | None:
    """Expose the deterministic successor for interfaces without mutating state."""

    return WorkflowStateMachine.next_state(workflow.state)
