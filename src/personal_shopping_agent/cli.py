"""Safe local administration CLI for health and packaged database migrations."""

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from personal_shopping_agent.application import (
    ArchiveTargetExistsError,
    ArchiveWriteError,
    WorkflowDataIntegrityError,
    WorkflowDataLifecycleService,
    WorkflowDataOwnershipError,
    WorkflowDeletionConfirmationError,
    WorkflowDeletionPlanStaleError,
)
from personal_shopping_agent.health import health_check
from personal_shopping_agent.runtime_settings import database_url_from_environment
from personal_shopping_agent.storage import (
    DatabaseMigrationError,
    DatabaseMigrationStatus,
    DatabaseSchemaNotReadyError,
    EntityNotFoundError,
    LocalJsonWorkflowArchiveWriter,
    SQLiteWorkflowDataStore,
    create_session_factory,
    create_sqlite_engine,
    inspect_database_migrations,
    require_current_database,
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
    data_parser = commands.add_parser(
        "data",
        help="export or explicitly delete one local workflow",
    )
    data_commands = data_parser.add_subparsers(dest="data_command", required=True)
    export_parser = data_commands.add_parser(
        "export",
        help="write one new private JSON archive without overwriting",
    )
    export_parser.add_argument("workflow_id")
    export_parser.add_argument("--output", required=True)
    delete_parser = data_commands.add_parser(
        "delete",
        help="preview deletion unless the fresh confirmation token is supplied",
    )
    delete_parser.add_argument("workflow_id")
    delete_parser.add_argument("--confirm")
    for data_command_parser in (export_parser, delete_parser):
        data_command_parser.add_argument(
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


def _emit_data_error(error_code: str, message: str, *, command: str) -> None:
    _emit(
        {
            "command": command,
            "error_code": error_code,
            "message": message,
            "ok": False,
        }
    )


def _run_data_command(
    namespace: argparse.Namespace,
    *,
    environment: Mapping[str, str] | None,
) -> int:
    data_command = cast(str, namespace.data_command)
    command = f"data_{data_command}"
    try:
        workflow_id = UUID(cast(str, namespace.workflow_id))
    except ValueError:
        _emit_data_error(
            "invalid_workflow_id",
            "Workflow ID must be a valid UUID.",
            command=command,
        )
        return 2

    explicit_database_url = cast(str | None, namespace.database_url)
    database_url = explicit_database_url or database_url_from_environment(environment)
    engine = None
    try:
        require_current_database(database_url)
        engine = create_sqlite_engine(database_url)
        service = WorkflowDataLifecycleService(
            SQLiteWorkflowDataStore(create_session_factory(engine)),
            LocalJsonWorkflowArchiveWriter(),
        )
        if data_command == "export":
            result = service.export(workflow_id, Path(cast(str, namespace.output)))
            _emit(
                {
                    "command": command,
                    "ok": True,
                    "result": result.model_dump(mode="json"),
                }
            )
            return 0

        confirmation = cast(str | None, namespace.confirm)
        if confirmation is None:
            plan = service.prepare_deletion(workflow_id)
            _emit(
                {
                    "command": command,
                    "executed": False,
                    "ok": True,
                    "plan": plan.model_dump(mode="json"),
                }
            )
            return 0
        result = service.delete(workflow_id, confirmation)
        _emit(
            {
                "command": command,
                "executed": True,
                "ok": True,
                "plan": result.plan.model_dump(mode="json"),
            }
        )
        return 0
    except ValueError as error:
        if isinstance(error, WorkflowDeletionConfirmationError):
            _emit_data_error(
                "confirmation_mismatch",
                "Confirmation does not match the current deletion preview.",
                command=command,
            )
        else:
            _emit_data_error(
                "invalid_database_url",
                "Only a valid local SQLite database URL is supported.",
                command=command,
            )
        return 2
    except DatabaseSchemaNotReadyError:
        _emit_data_error(
            "database_not_ready",
            "Run `personal-shopping-agent migrate` before managing workflow data.",
            command=command,
        )
        return 1
    except EntityNotFoundError:
        _emit_data_error(
            "workflow_not_found",
            "The local shopping workflow was not found.",
            command=command,
        )
        return 1
    except ArchiveTargetExistsError:
        _emit_data_error(
            "export_target_exists",
            "The export target already exists and will not be overwritten.",
            command=command,
        )
        return 1
    except ArchiveWriteError:
        _emit_data_error(
            "archive_write_failed",
            "The private workflow archive could not be written.",
            command=command,
        )
        return 2
    except WorkflowDeletionPlanStaleError:
        _emit_data_error(
            "deletion_plan_stale",
            "Workflow data changed; request a new deletion preview.",
            command=command,
        )
        return 1
    except (WorkflowDataIntegrityError, WorkflowDataOwnershipError):
        _emit_data_error(
            "workflow_data_integrity_failed",
            "The workflow data cannot be safely exported or deleted.",
            command=command,
        )
        return 2
    except (DatabaseMigrationError, SQLAlchemyError):
        _emit_data_error(
            "database_operation_failed",
            "The local database operation failed.",
            command=command,
        )
        return 2
    finally:
        if engine is not None:
            engine.dispose()


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

    if command_name == "data":
        return _run_data_command(namespace, environment=environment)

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
