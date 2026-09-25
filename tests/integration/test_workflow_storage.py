"""Integration tests for transactional workflow storage and application service."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, text

from personal_shopping_agent.automation import (
    ShoppingWorkflowService,
)
from personal_shopping_agent.domain import (
    Budget,
    Money,
    ShoppingRequest,
    ShoppingWorkflow,
    WorkflowState,
    WorkflowStateMachine,
)
from personal_shopping_agent.infrastructure.storage import (
    ConcurrentWorkflowUpdateError,
    DuplicateEntityError,
    EntityNotFoundError,
    InvalidReferenceError,
    SQLiteWorkflowRepository,
    create_schema,
    create_session_factory,
    create_sqlite_engine,
)
from personal_shopping_agent.infrastructure.storage.tables import ShoppingRequestRecord

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def build_request() -> ShoppingRequest:
    return ShoppingRequest(
        query="预算五千元、续航优先的手机",
        category="smartphone",
        budget=Budget(maximum=Money(amount=Decimal("5000"))),
        region="云南省曲靖市",
        created_at=NOW,
    )


def test_service_starts_reads_advances_and_fails_workflows() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    repository = SQLiteWorkflowRepository(create_session_factory(engine))
    service = ShoppingWorkflowService(repository, clock=lambda: NOW)
    request = build_request()

    started = service.start(request)
    assert service.get(started.workflow.id) == started

    advanced = service.advance(
        started.workflow.id,
        WorkflowState.CANDIDATES_DISCOVERED,
        reason="candidate discovery completed",
    )
    assert advanced.workflow.revision == 2
    assert advanced.workflow.state is WorkflowState.CANDIDATES_DISCOVERED
    assert [event.sequence for event in advanced.events] == [0, 1, 2]

    failed = service.fail(
        advanced.workflow.id,
        error_code="source_unavailable",
        error_message="A required source is temporarily unavailable.",
    )
    assert failed.workflow.state is WorkflowState.FAILED
    assert failed.workflow.revision == 3
    assert [event.sequence for event in failed.events] == [0, 1, 2, 3]
    engine.dispose()


def test_repository_rejects_duplicates_invalid_references_and_missing_ids() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    repository = SQLiteWorkflowRepository(create_session_factory(engine))
    request = build_request()
    workflow, events = WorkflowStateMachine().initialize(request.id, occurred_at=NOW)
    repository.start(request, workflow, events)

    with pytest.raises(DuplicateEntityError):
        repository.start(request, workflow, events)
    with pytest.raises(EntityNotFoundError, match="shopping_workflows"):
        repository.get_snapshot(uuid4())

    other_request = build_request()
    invalid_workflow = ShoppingWorkflow(
        request_id=uuid4(),
        state=WorkflowState.REQUEST_VALIDATED,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
    )
    invalid_events = WorkflowStateMachine().initialize(
        invalid_workflow.request_id, occurred_at=NOW
    )[1]
    invalid_events = tuple(
        event.model_copy(update={"workflow_id": invalid_workflow.id}) for event in invalid_events
    )
    with pytest.raises(InvalidReferenceError):
        repository.start(other_request, invalid_workflow, invalid_events)

    engine.dispose()


def test_repository_detects_stale_workflow_revision() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    repository = SQLiteWorkflowRepository(create_session_factory(engine))
    request = build_request()
    machine = WorkflowStateMachine()
    workflow, events = machine.initialize(request.id, occurred_at=NOW)
    repository.start(request, workflow, events)
    updated, event = machine.advance(
        workflow,
        WorkflowState.CANDIDATES_DISCOVERED,
        reason="discovered",
        occurred_at=NOW,
    )
    repository.save_transition(updated, event, expected_revision=workflow.revision)

    stale_update, stale_event = machine.advance(
        workflow,
        WorkflowState.CANDIDATES_DISCOVERED,
        reason="stale duplicate",
        occurred_at=NOW,
    )
    with pytest.raises(ConcurrentWorkflowUpdateError):
        repository.save_transition(
            stale_update,
            stale_event,
            expected_revision=workflow.revision,
        )

    engine.dispose()


def test_repository_detects_corrupted_missing_request() -> None:
    engine = create_sqlite_engine("sqlite://")
    create_schema(engine)
    factory = create_session_factory(engine)
    repository = SQLiteWorkflowRepository(factory)
    request = build_request()
    workflow, events = WorkflowStateMachine().initialize(request.id, occurred_at=NOW)
    repository.start(request, workflow, events)

    with engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(
            delete(ShoppingRequestRecord).where(ShoppingRequestRecord.id == str(request.id))
        )
        connection.commit()

    with pytest.raises(EntityNotFoundError, match="workflow request"):
        repository.get_snapshot(workflow.id)

    engine.dispose()
