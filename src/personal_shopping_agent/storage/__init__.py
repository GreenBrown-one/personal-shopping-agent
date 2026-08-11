"""Public local-storage adapter API."""

from personal_shopping_agent.storage.archive import LocalJsonWorkflowArchiveWriter
from personal_shopping_agent.storage.collection_unit_of_work import (
    SQLiteSearchDetailUnitOfWork,
)
from personal_shopping_agent.storage.cross_check_unit_of_work import (
    SQLiteEvidenceCrossCheckUnitOfWork,
)
from personal_shopping_agent.storage.data_lifecycle import SQLiteWorkflowDataStore
from personal_shopping_agent.storage.database import (
    create_schema,
    create_session_factory,
    create_sqlite_engine,
    session_scope,
)
from personal_shopping_agent.storage.ingestion_unit_of_work import (
    SQLiteOfferIngestionUnitOfWork,
)
from personal_shopping_agent.storage.migrations import (
    DatabaseMigrationError,
    DatabaseMigrationStatus,
    DatabaseSchemaNotReadyError,
    create_migration_config,
    inspect_database_migrations,
    migration_script_location,
    require_current_database,
    upgrade_database,
)
from personal_shopping_agent.storage.normalization_unit_of_work import (
    SQLiteSpecificationNormalizationUnitOfWork,
)
from personal_shopping_agent.storage.observation_repository import (
    ObservationKindMismatchError,
    SQLitePlatformObservationRepository,
)
from personal_shopping_agent.storage.reporting_repository import (
    SQLiteShoppingReportRepository,
)
from personal_shopping_agent.storage.reporting_unit_of_work import (
    SQLiteShoppingReportUnitOfWork,
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
    "DatabaseMigrationError",
    "DatabaseMigrationStatus",
    "DatabaseSchemaNotReadyError",
    "DuplicateEntityError",
    "EntityNotFoundError",
    "InvalidReferenceError",
    "LocalJsonWorkflowArchiveWriter",
    "ObservationKindMismatchError",
    "SQLiteCandidateScoringUnitOfWork",
    "SQLiteEvidenceCrossCheckUnitOfWork",
    "SQLiteOfferIngestionUnitOfWork",
    "SQLitePlatformObservationRepository",
    "SQLiteSearchDetailUnitOfWork",
    "SQLiteShoppingReportRepository",
    "SQLiteShoppingReportUnitOfWork",
    "SQLiteShoppingRepository",
    "SQLiteSpecificationNormalizationUnitOfWork",
    "SQLiteWorkflowDataStore",
    "SQLiteWorkflowRepository",
    "create_migration_config",
    "create_schema",
    "create_session_factory",
    "create_sqlite_engine",
    "inspect_database_migrations",
    "migration_script_location",
    "require_current_database",
    "session_scope",
    "upgrade_database",
]
