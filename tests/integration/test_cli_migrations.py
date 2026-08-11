"""Integration proof for packaged migration administration and startup readiness."""

import json
from pathlib import Path

import pytest
from alembic.config import Config

import personal_shopping_agent.cli as cli_module
import personal_shopping_agent.storage.migrations as migration_module
from personal_shopping_agent.cli import main
from personal_shopping_agent.mcp import create_default_server
from personal_shopping_agent.runtime_settings import DATABASE_URL_ENV
from personal_shopping_agent.storage import (
    DatabaseMigrationError,
    DatabaseMigrationStatus,
    DatabaseSchemaNotReadyError,
    inspect_database_migrations,
    require_current_database,
)


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_cli_health_and_missing_database_doctor_are_non_destructive(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "missing" / "shopping.db"
    database_url = f"sqlite:///{database_path}"

    assert main(["health"], environment={}) == 0
    assert _output(capsys) == {
        "service": "personal-shopping-agent",
        "status": "ok",
        "version": "0.1.0",
    }

    assert main(["doctor"], environment={DATABASE_URL_ENV: database_url}) == 1
    payload = _output(capsys)
    assert payload["ok"] is False
    assert payload["database"] == {
        "database_exists": False,
        "current_revision": None,
        "target_revision": "20260809_0007",
        "ready": False,
    }
    assert not database_path.exists()
    with pytest.raises(DatabaseSchemaNotReadyError, match="migrate"):
        require_current_database(database_url)
    with pytest.raises(DatabaseSchemaNotReadyError, match="migrate"):
        create_default_server({DATABASE_URL_ENV: database_url})


def test_cli_migrate_creates_parent_and_default_server_accepts_current_schema(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "nested" / "shopping.db"
    database_url = f"sqlite:///{database_path}"

    assert main(["migrate", "--database-url", database_url], environment={}) == 0
    migrated = _output(capsys)
    assert migrated == {
        "command": "migrate",
        "database": {
            "database_exists": True,
            "current_revision": "20260809_0007",
            "target_revision": "20260809_0007",
            "ready": True,
        },
        "ok": True,
    }
    assert database_path.is_file()

    assert main(["doctor", "--database-url", database_url], environment={}) == 0
    assert _output(capsys)["ok"] is True
    assert require_current_database(database_url).ready is True
    server = create_default_server({DATABASE_URL_ENV: database_url})
    assert server.name == "personal-shopping-agent"


@pytest.mark.parametrize("database_url", ["not a url", "postgresql://localhost/shopping"])
def test_cli_rejects_invalid_or_non_sqlite_database_urls(
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["doctor", "--database-url", database_url], environment={}) == 2
    assert _output(capsys) == {
        "command": "doctor",
        "error_code": "invalid_database_url",
        "message": "Only a valid local SQLite database URL is supported.",
        "ok": False,
    }


def test_cli_sanitizes_migration_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_: str) -> None:
        raise DatabaseMigrationError("sensitive internal detail")

    monkeypatch.setattr(cli_module, "inspect_database_migrations", fail)

    assert main(["doctor"], environment={}) == 2
    assert _output(capsys) == {
        "command": "doctor",
        "error_code": "database_migration_failed",
        "message": "The local database migration operation failed.",
        "ok": False,
    }


def test_in_memory_database_status_is_supported_without_claiming_readiness() -> None:
    status = inspect_database_migrations("sqlite://")

    assert status.database_exists is True
    assert status.current_revision is None
    assert status.ready is False


def test_migration_inspection_sanitizes_missing_history_and_internal_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoHeadScript:
        def get_current_head(self) -> None:
            return None

    def no_head(_: Config) -> NoHeadScript:
        return NoHeadScript()

    monkeypatch.setattr(
        migration_module.ScriptDirectory,
        "from_config",
        no_head,
    )
    with pytest.raises(DatabaseMigrationError, match="no target"):
        inspect_database_migrations("sqlite://")

    def fail(_: object) -> None:
        raise RuntimeError("internal detail")

    monkeypatch.setattr(migration_module.ScriptDirectory, "from_config", fail)
    with pytest.raises(DatabaseMigrationError, match="could not be inspected"):
        inspect_database_migrations("sqlite://")


def test_migration_upgrade_sanitizes_command_failure_and_unreached_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'failed.db'}"

    def fail_command(_: object, __: str) -> None:
        raise RuntimeError("internal detail")

    monkeypatch.setattr(migration_module.command, "upgrade", fail_command)
    with pytest.raises(DatabaseMigrationError, match="could not be completed"):
        migration_module.upgrade_database(database_url)

    def succeed_command(_: Config, __: str) -> None:
        return None

    monkeypatch.setattr(migration_module.command, "upgrade", succeed_command)
    not_ready = DatabaseMigrationStatus(
        database_exists=True,
        current_revision=None,
        target_revision="head",
        ready=False,
    )

    def inspect_not_ready(_: str) -> DatabaseMigrationStatus:
        return not_ready

    monkeypatch.setattr(migration_module, "inspect_database_migrations", inspect_not_ready)
    with pytest.raises(DatabaseMigrationError, match="did not reach"):
        migration_module.upgrade_database("sqlite://")
