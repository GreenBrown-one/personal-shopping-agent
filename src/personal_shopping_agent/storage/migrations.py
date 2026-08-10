"""Packaged Alembic migration access and fail-closed schema readiness checks."""

from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from personal_shopping_agent.storage.database import create_sqlite_engine


class DatabaseMigrationError(RuntimeError):
    """Sanitized failure raised when migration inspection or upgrade cannot complete."""


class DatabaseSchemaNotReadyError(DatabaseMigrationError):
    """Raised when an MCP process is pointed at an absent or outdated schema."""


@dataclass(frozen=True, slots=True)
class DatabaseMigrationStatus:
    """Non-sensitive database revision state suitable for CLI diagnostics."""

    database_exists: bool
    current_revision: str | None
    target_revision: str
    ready: bool


def migration_script_location() -> Path:
    """Resolve migrations from an installed wheel or the source checkout."""

    package_location = Path(__file__).resolve().parents[1] / "migrations"
    if package_location.is_dir():  # pragma: no cover - exercised by built-wheel smoke test
        return package_location
    source_location = Path(__file__).resolve().parents[3] / "migrations"
    if not source_location.is_dir():  # pragma: no cover - defensive broken-package path
        raise DatabaseMigrationError("Packaged database migrations are unavailable.")
    return source_location


def create_migration_config(database_url: str) -> Config:
    """Create an Alembic configuration independent of a working-directory ini file."""

    _database_file_path(database_url)
    config = Config()
    config.set_main_option(
        "script_location",
        str(migration_script_location()).replace("%", "%%"),
    )
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def inspect_database_migrations(database_url: str) -> DatabaseMigrationStatus:
    """Read schema revision without creating a missing file-backed database."""

    database_path = _database_file_path(database_url)
    database_exists = database_path is None or database_path.is_file()
    try:
        config = create_migration_config(database_url)
        target_revision = ScriptDirectory.from_config(config).get_current_head()
        if target_revision is None:
            raise DatabaseMigrationError("The migration history has no target revision.")
        if not database_exists:
            return DatabaseMigrationStatus(
                database_exists=False,
                current_revision=None,
                target_revision=target_revision,
                ready=False,
            )

        engine = create_sqlite_engine(database_url)
        try:
            with engine.connect() as connection:
                current_revision = MigrationContext.configure(connection).get_current_revision()
        finally:
            engine.dispose()
    except DatabaseMigrationError:
        raise
    except Exception as error:
        raise DatabaseMigrationError("Database migration status could not be inspected.") from error

    return DatabaseMigrationStatus(
        database_exists=True,
        current_revision=current_revision,
        target_revision=target_revision,
        ready=current_revision == target_revision,
    )


def upgrade_database(database_url: str) -> DatabaseMigrationStatus:
    """Apply forward-only Alembic migrations and return the verified current status."""

    database_path = _database_file_path(database_url)
    if database_path is not None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        command.upgrade(create_migration_config(database_url), "head")
    except Exception as error:
        raise DatabaseMigrationError("Database migration could not be completed.") from error

    status = inspect_database_migrations(database_url)
    if not status.ready:
        raise DatabaseMigrationError("Database migration did not reach the target revision.")
    return status


def require_current_database(database_url: str) -> DatabaseMigrationStatus:
    """Reject process startup until the configured database is at the packaged head."""

    status = inspect_database_migrations(database_url)
    if not status.ready:
        raise DatabaseSchemaNotReadyError(
            "Database schema is not ready. Run `personal-shopping-agent migrate` first."
        )
    return status


def _database_file_path(database_url: str) -> Path | None:
    try:
        url = make_url(database_url)
    except ArgumentError as error:
        raise ValueError("database URL is invalid") from error
    if not url.drivername.startswith("sqlite"):
        raise ValueError("only SQLite database URLs are supported")
    database = url.database
    if database is None or database in {"", ":memory:"}:
        return None
    return Path(database)
