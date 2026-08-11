"""Public local-storage adapter API."""

from personal_shopping_agent.storage.collection_unit_of_work import (
    SQLiteSearchDetailUnitOfWork,
)
from personal_shopping_agent.storage.cross_check_unit_of_work import (
    SQLiteEvidenceCrossCheckUnitOfWork,
)
from personal_shopping_agent.storage.database import (
    create_schema,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from personal_shopping_agent.storage.ingestion_unit_of_work import (
    SQLiteOfferIngestionUnitOfWork,
)
from personal_shopping_agent.storage.normalization_unit_of_work import (
    SQLiteSpecificationNormalizationUnitOfWork,
)
from personal_shopping_agent.storage.observation_repository import (
    ObservationKindMismatchError,
    SQLitePlatformObservationRepository,
)
from personal_shopping_agent.storage.repository import (
    DuplicateEntityError,
    EntityNotFoundError,
    InvalidReferenceError,
    SQLiteShoppingRepository,
)
from personal_shopping_agent.storage.scoring_unit_of_work import (
    SQLiteCandidateScoringUnitOfWork,
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
    "ObservationKindMismatchError",
    "SQLiteCandidateScoringUnitOfWork",
    "SQLiteEvidenceCrossCheckUnitOfWork",
    "SQLiteOfferIngestionUnitOfWork",
    "SQLitePlatformObservationRepository",
    "SQLiteSearchDetailUnitOfWork",
    "SQLiteShoppingRepository",
    "SQLiteSpecificationNormalizationUnitOfWork",
    "SQLiteWorkflowRepository",
    "create_schema",
    "create_session_factory",
    "create_sqlite_engine",
    "session_scope",
]
