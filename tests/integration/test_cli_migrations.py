"""Integration proof for packaged migration administration and startup readiness."""

import json
import os
import stat
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config

import personal_shopping_agent.infrastructure.storage.migrations as migration_module
import personal_shopping_agent.interfaces.cli as cli_module
from personal_shopping_agent import __version__
from personal_shopping_agent.infrastructure.local_security import LocalFileSecurityError
from personal_shopping_agent.infrastructure.settings import DATABASE_URL_ENV, LIVE_JD_ACCESS_ENV
from personal_shopping_agent.infrastructure.storage import (
    DatabaseMigrationError,
    DatabaseMigrationStatus,
    DatabaseSchemaNotReadyError,
    inspect_database_migrations,
    require_current_database,
)
from personal_shopping_agent.interfaces.cli import main
from personal_shopping_agent.interfaces.mcp import create_default_server


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
        "version": __version__,
    }

    assert main(["doctor"], environment={DATABASE_URL_ENV: database_url}) == 1
    payload = _output(capsys)
    assert payload["ok"] is False
    assert payload["database"] == {
        "database_exists": False,
        "current_revision": None,
        "private_file_permissions": None,
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
            "private_file_permissions": True if os.name == "posix" else None,
            "target_revision": "20260809_0007",
            "ready": True,
        },
        "ok": True,
    }
    assert database_path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(database_path.parent.stat().st_mode) == 0o700

    assert main(["doctor", "--database-url", database_url], environment={}) == 0
    assert _output(capsys)["ok"] is True
    assert require_current_database(database_url).ready is True
    server = create_default_server({DATABASE_URL_ENV: database_url})
    assert server.name == "personal-shopping-agent"


def test_cli_generates_reviewable_default_and_live_mcp_configurations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")
    (tmp_path / "src" / "personal_shopping_agent").mkdir(parents=True)
    project_directory = str(tmp_path.resolve())

    assert main(["mcp-config", "--project-directory", project_directory]) == 0
    offline = _output(capsys)
    assert offline["command"] == "mcp_config"
    assert offline["live_jd"] is False
    assert offline["ok"] is True
    assert offline["warning"] == "The default server does not access shopping platforms."
    config = offline["config"]
    assert isinstance(config, dict)
    servers = cast(dict[str, object], config["mcpServers"])
    details = cast(dict[str, object], servers["personal-shopping-agent"])
    args = cast(list[str], details["args"])
    assert args[-1] == "personal-shopping-agent-mcp"

    assert main(["mcp-config", "--project-directory", project_directory, "--live-jd"]) == 0
    live = _output(capsys)
    assert live["live_jd"] is True
    assert live["warning"] == (
        "Live JD access still requires Chromium installation and manual acceptance."
    )
    live_config = live["config"]
    assert isinstance(live_config, dict)
    live_servers = cast(dict[str, object], live_config["mcpServers"])
    live_details = cast(dict[str, object], live_servers["personal-shopping-agent"])
    live_args = cast(list[str], live_details["args"])
    live_environment = cast(dict[str, str], live_details["env"])
    assert live_args[-1] == "personal-shopping-agent-mcp-jd"
    assert live_environment[LIVE_JD_ACCESS_ENV] == "true"


def test_cli_sanitizes_an_invalid_mcp_project_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "sensitive-name"

    assert main(["mcp-config", "--project-directory", str(missing)]) == 2
    assert _output(capsys) == {
        "command": "mcp_config",
        "error_code": "source_checkout_invalid",
        "message": "Select a complete Personal Shopping Agent source checkout.",
        "ok": False,
    }


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
    assert status.private_file_permissions is None
    assert status.ready is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are unavailable")
def test_doctor_fails_closed_and_migrate_repairs_broad_database_permissions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "permissions.db"
    database_url = f"sqlite:///{database_path}"
    assert main(["migrate", "--database-url", database_url], environment={}) == 0
    _output(capsys)
    database_path.chmod(0o644)

    assert main(["doctor", "--database-url", database_url], environment={}) == 1
    unsafe = _output(capsys)
    assert unsafe["database"] == {
        "database_exists": True,
        "current_revision": None,
        "private_file_permissions": False,
        "target_revision": "20260809_0007",
        "ready": False,
    }
    with pytest.raises(DatabaseSchemaNotReadyError):
        require_current_database(database_url)

    assert main(["migrate", "--database-url", database_url], environment={}) == 0
    repaired = _output(capsys)
    repaired_database = repaired["database"]
    assert isinstance(repaired_database, dict)
    assert repaired_database["private_file_permissions"] is True
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600


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


def test_migration_upgrade_sanitizes_private_file_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_security(_: Path) -> None:
        raise LocalFileSecurityError("sensitive permission detail")

    monkeypatch.setattr(migration_module, "prepare_private_file", fail_security)
    with pytest.raises(DatabaseMigrationError, match="private file") as captured:
        migration_module.upgrade_database(f"sqlite:///{tmp_path / 'private.db'}")
    assert "sensitive permission detail" not in str(captured.value)
