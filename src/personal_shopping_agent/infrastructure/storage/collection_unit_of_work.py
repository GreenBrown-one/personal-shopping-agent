"""Request-scoped SQLite work unit for atomic search/detail collection."""

from types import TracebackType
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from personal_shopping_agent.application import (
    DetailObservation,
    SearchObservation,
    ShoppingWorkflow,
    WorkflowEvent,
    WorkflowSnapshot,
)
from personal_shopping_agent.storage.observation_repository import (
    detail_observation_record,
    search_observation_record,
)
from personal_shopping_agent.storage.workflow_repository import (
    apply_workflow_transition,
    load_workflow_snapshot,
)


class SQLiteSearchDetailUnitOfWork:
    """Hold one SQLite session and connection across a complete collection batch."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._active_session: Session | None = None
        self._committed = False

    def __enter__(self) -> "SQLiteSearchDetailUnitOfWork":
        session = self._session_factory()
        session.connection()
        self._active_session = session
        self._committed = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        session = self._session()
        try:
            if exc_type is not None or not self._committed:
                session.rollback()
        finally:
            session.close()
            self._active_session = None
            self._committed = False

    def get_snapshot(self, workflow_id: UUID) -> WorkflowSnapshot:
        """Load a workflow and request through the active request connection."""

        return load_workflow_snapshot(self._session(), workflow_id)

    def add_search_observation(self, observation: SearchObservation) -> None:
        """Stage one search observation in the active transaction."""

        self._session().add(search_observation_record(observation))

    def add_detail_observation(self, observation: DetailObservation) -> None:
        """Stage one detail observation in the active transaction."""

        self._session().add(detail_observation_record(observation))

    def save_transition(
        self,
        workflow: ShoppingWorkflow,
        event: WorkflowEvent,
        *,
        expected_revision: int,
    ) -> None:
        """Stage the conservative collection transition in the same transaction."""

        apply_workflow_transition(
            self._session(),
            workflow,
            event,
            expected_revision=expected_revision,
        )

    def commit(self) -> None:
        """Commit every staged observation and workflow event atomically."""

        self._session().commit()
        self._committed = True

    def _session(self) -> Session:
        if self._active_session is None:
            raise RuntimeError("search/detail unit of work is not active")
        return self._active_session
