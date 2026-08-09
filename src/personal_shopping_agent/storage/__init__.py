"""Public local-storage adapter API."""

from personal_shopping_agent.storage.database import (
    create_schema,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from personal_shopping_agent.storage.repository import (
    DuplicateEntityError,
    EntityNotFoundError,
    InvalidReferenceError,
    SQLiteShoppingRepository,
)
from personal_shopping_agent.storage.workflow_repository import (
    ConcurrentWorkflowUpdateError,
    SQLiteWorkflowRepository,
)

__all__ = [
    "ConcurrentWorkflowUpdateError",
    "DuplicateEntityError",
    "EntityNotFoundError",
    "InvalidReferenceError",
    "SQLiteShoppingRepository",
    "SQLiteWorkflowRepository",
    "create_schema",
    "create_session_factory",
    "create_sqlite_engine",
    "session_scope",
]
