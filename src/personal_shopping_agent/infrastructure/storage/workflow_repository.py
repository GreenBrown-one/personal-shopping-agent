"""Transactional SQLite persistence for workflow snapshots and audit events."""

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.domain import ShoppingRequest
from personal_shopping_agent.domain.workflow import (
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
)
from personal_shopping_agent.infrastructure.storage.database import session_scope
from personal_shopping_agent.infrastructure.storage.repository import (
    DuplicateEntityError,
    EntityNotFoundError,
    InvalidReferenceError,
    domain_payload,
)
from personal_shopping_agent.infrastructure.storage.tables import (
    ShoppingRequestRecord,
    WorkflowEventRecord,
    WorkflowRecord,
)


class ConcurrentWorkflowUpdateError(RuntimeError):
    """Raised when optimistic revision checking detects a stale transition."""


def workflow_record(workflow: ShoppingWorkflow) -> WorkflowRecord:
    """Map a validated workflow to its persistence record."""

    return WorkflowRecord(
        id=str(workflow.id),
        request_id=str(workflow.request_id),
        state=workflow.state.value,
        revision=workflow.revision,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        error_code=workflow.error_code,
        error_message=workflow.error_message,
        payload=domain_payload(workflow),
    )


def event_record(event: WorkflowEvent) -> WorkflowEventRecord:
    """Map a validated workflow event to its append-only persistence record."""

    return WorkflowEventRecord(
        id=str(event.id),
        workflow_id=str(event.workflow_id),
        sequence=event.sequence,
        from_state=event.from_state.value if event.from_state is not None else None,
        to_state=event.to_state.value,
        occurred_at=event.occurred_at,
        reason=event.reason,
        payload=domain_payload(event),
    )


def request_record(request: ShoppingRequest) -> ShoppingRequestRecord:
    """Map a validated request for atomic workflow initialization."""

    return ShoppingRequestRecord(
        id=str(request.id),
        query=request.query,
        category=request.category,
        region=request.region,
        created_at=request.created_at,
        payload=domain_payload(request),
    )


def load_workflow_snapshot(session: Session, workflow_id: UUID) -> WorkflowSnapshot:
    """Load and revalidate a complete workflow snapshot in an existing transaction."""

    workflow = session.get(WorkflowRecord, str(workflow_id))
    if workflow is None:
        raise EntityNotFoundError("shopping_workflows entity was not found")
    request = session.get(ShoppingRequestRecord, workflow.request_id)
    if request is None:
        raise EntityNotFoundError("workflow request entity was not found")
    statement = (
        select(WorkflowEventRecord)
        .where(WorkflowEventRecord.workflow_id == str(workflow_id))
        .order_by(WorkflowEventRecord.sequence)
    )
    events = session.scalars(statement).all()
    return WorkflowSnapshot(
        request=ShoppingRequest.model_validate(request.payload),
        workflow=ShoppingWorkflow.model_validate(workflow.payload),
        events=tuple(WorkflowEvent.model_validate(event.payload) for event in events),
    )


def apply_workflow_transition(
    session: Session,
    workflow: ShoppingWorkflow,
    event: WorkflowEvent,
    *,
    expected_revision: int,
) -> None:
    """Apply one optimistic workflow transition inside an existing transaction."""

    statement = (
        update(WorkflowRecord)
        .where(
            WorkflowRecord.id == str(workflow.id),
            WorkflowRecord.revision == expected_revision,
        )
        .values(
            state=workflow.state.value,
            revision=workflow.revision,
            updated_at=workflow.updated_at,
            error_code=workflow.error_code,
            error_message=workflow.error_message,
            payload=domain_payload(workflow),
        )
    )
    result = cast(CursorResult[Any], session.execute(statement))
    if result.rowcount != 1:
        raise ConcurrentWorkflowUpdateError("workflow revision is stale")
    session.add(event_record(event))


class SQLiteWorkflowRepository:
    """Durable workflow store with atomic start and optimistic transitions."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def start(
        self,
        request: ShoppingRequest,
        workflow: ShoppingWorkflow,
        events: tuple[WorkflowEvent, ...],
    ) -> None:
        """Persist request, current workflow, and initial events in one transaction."""

        try:
            with session_scope(self._session_factory) as session:
                session.add(request_record(request))
                session.flush()
                session.add(workflow_record(workflow))
                session.flush()
                session.add_all([event_record(event) for event in events])
        except IntegrityError as exc:
            if "FOREIGN KEY constraint failed" in str(exc.orig):
                raise InvalidReferenceError("workflow references an unknown entity") from exc
            raise DuplicateEntityError("workflow or request identifier already exists") from exc

    def get_snapshot(self, workflow_id: UUID) -> WorkflowSnapshot:
        """Load and revalidate the request, workflow, and ordered audit trail."""

        with self._session_factory() as session:
            return load_workflow_snapshot(session, workflow_id)

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None:
        """Atomically update current state and append an event if the revision is current."""

        with session_scope(self._session_factory) as session:
            apply_workflow_transition(
                session,
                workflow,
                event,
                expected_revision=expected_revision,
            )
