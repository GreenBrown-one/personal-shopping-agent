"""Safe local administration CLI for health and packaged database migrations."""

import argparse
import json
from collections.abc import Mapping, Sequence
from typing import cast

from personal_shopping_agent.health import health_check
from personal_shopping_agent.runtime_settings import database_url_from_environment
from personal_shopping_agent.storage import (
    DatabaseMigrationError,
    DatabaseMigrationStatus,
    inspect_database_migrations,
    upgrade_database,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="personal-shopping-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="print the process health contract")
    for name, help_text in (
        ("doctor", "check whether the local database schema is current"),
        ("migrate", "apply packaged forward database migrations"),
    ):
        command_parser = commands.add_parser(name, help=help_text)
        command_parser.add_argument(
            "--database-url",
            help="local SQLite URL; defaults to PERSONAL_SHOPPING_DATABASE_URL",
        )
    return parser


def _status_payload(status: DatabaseMigrationStatus) -> dict[str, object]:
    return {
        "database_exists": status.database_exists,
        "current_revision": status.current_revision,
        "target_revision": status.target_revision,
        "ready": status.ready,
    }


def _emit(payload: Mapping[str, object]) -> None:
    print(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True))


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Execute one non-interactive command and return a process exit code."""

    namespace = _build_parser().parse_args(list(argv) if argv is not None else None)
    command_name = cast(str, namespace.command)
    if command_name == "health":
        health = health_check()
        _emit(
            {
                "service": health.service,
                "status": health.status,
                "version": health.version,
            }
        )
        return 0

    explicit_database_url = cast(str | None, namespace.database_url)
    database_url = explicit_database_url or database_url_from_environment(environment)
    try:
        status = (
            upgrade_database(database_url)
            if command_name == "migrate"
            else inspect_database_migrations(database_url)
        )
    except ValueError:
        _emit(
            {
                "command": command_name,
                "error_code": "invalid_database_url",
                "message": "Only a valid local SQLite database URL is supported.",
                "ok": False,
            }
        )
        return 2
    except DatabaseMigrationError:
        _emit(
            {
                "command": command_name,
                "error_code": "database_migration_failed",
                "message": "The local database migration operation failed.",
                "ok": False,
            }
        )
        return 2

    _emit(
        {
            "command": command_name,
            "database": _status_payload(status),
            "ok": status.ready,
        }
    )
    return 0 if status.ready else 1
