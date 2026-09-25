"""Safe local administration CLI: health, migrations, data lifecycle, and improvement cases."""

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from personal_shopping_agent.automation import (
    ArchiveTargetExistsError,
    ArchiveWriteError,
    WorkflowDataIntegrityError,
    WorkflowDataLifecycleService,
    WorkflowDataOwnershipError,
    WorkflowDeletionConfirmationError,
    WorkflowDeletionPlanStaleError,
)
from personal_shopping_agent.evolution import ImprovementCaseBuilder, is_stable_code
from personal_shopping_agent.infrastructure.settings import database_url_from_environment
from personal_shopping_agent.infrastructure.storage import (
    DatabaseMigrationError,
    DatabaseMigrationStatus,
    DatabaseSchemaNotReadyError,
    EntityNotFoundError,
    LocalJsonWorkflowArchiveWriter,
    SQLiteWorkflowDataStore,
    SQLiteWorkflowRepository,
    create_session_factory,
    create_sqlite_engine,
    inspect_database_migrations,
    require_current_database,
    upgrade_database,
)
from personal_shopping_agent.interfaces.health import health_check
from personal_shopping_agent.interfaces.host_config import (
    SourceCheckoutError,
    build_source_mcp_configuration,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="personal-shopping-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="print the process health contract")
    mcp_config_parser = commands.add_parser(
        "mcp-config",
        help="print a secret-free generic stdio MCP configuration",
    )
    mcp_config_parser.add_argument(
        "--project-directory",
        default=".",
        help="complete source checkout; defaults to the current directory",
    )
    mcp_config_parser.add_argument(
        "--live-jd",
        action="store_true",
        help="select the explicit live-JD entry point instead of the offline default",
    )
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
    improvement_parser = commands.add_parser(
        "improvement-case",
        help="print a local sanitized improvement case preview; nothing is uploaded",
    )
    improvement_parser.add_argument("workflow_id")
    improvement_parser.add_argument(
        "--error-code",
        help="stable error code the user saw, e.g. platform_access_restricted",
    )
    for data_command_parser in (export_parser, delete_parser, improvement_parser):
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
        "private_file_permissions": status.private_file_permissions,
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


def _run_mcp_config(namespace: argparse.Namespace) -> int:
    live_jd = cast(bool, namespace.live_jd)
    try:
        configuration = build_source_mcp_configuration(
            Path(cast(str, namespace.project_directory)),
            live_jd=live_jd,
        )
    except SourceCheckoutError:
        _emit(
            {
                "command": "mcp_config",
                "error_code": "source_checkout_invalid",
                "message": "Select a complete Personal Shopping Agent source checkout.",
                "ok": False,
            }
        )
        return 2
    _emit(
        {
            "command": "mcp_config",
            "config": configuration,
            "live_jd": live_jd,
            "ok": True,
            "warning": (
                "Live JD access still requires Chromium installation and manual acceptance."
                if live_jd
                else "The default server does not access shopping platforms."
            ),
        }
    )
    return 0


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


def _run_improvement_case(
    namespace: argparse.Namespace,
    *,
    environment: Mapping[str, str] | None,
) -> int:
    command = "improvement_case"
    try:
        workflow_id = UUID(cast(str, namespace.workflow_id))
    except ValueError:
        _emit_data_error(
            "invalid_workflow_id",
            "Workflow ID must be a valid UUID.",
            command=command,
        )
        return 2
    reported_error_code = cast(str | None, namespace.error_code)
    if reported_error_code is not None and not is_stable_code(reported_error_code):
        _emit_data_error(
            "invalid_error_code",
            "Error codes may contain only lowercase letters, digits, and _.:-",
            command=command,
        )
        return 2

    explicit_database_url = cast(str | None, namespace.database_url)
    database_url = explicit_database_url or database_url_from_environment(environment)
    try:
        require_current_database(database_url)
    except ValueError:
        _emit_data_error(
            "invalid_database_url",
            "Only a valid local SQLite database URL is supported.",
            command=command,
        )
        return 2
    except DatabaseSchemaNotReadyError:
        _emit_data_error(
            "database_not_ready",
            "Run `personal-shopping-agent migrate` before building an improvement case.",
            command=command,
        )
        return 1
    except DatabaseMigrationError:
        _emit_data_error(
            "database_operation_failed",
            "The local database operation failed.",
            command=command,
        )
        return 2

    engine = create_sqlite_engine(database_url)
    try:
        snapshot = SQLiteWorkflowRepository(create_session_factory(engine)).get_snapshot(
            workflow_id
        )
    except EntityNotFoundError:
        _emit_data_error(
            "workflow_not_found",
            "The local shopping workflow was not found.",
            command=command,
        )
        return 1
    except SQLAlchemyError:
        _emit_data_error(
            "database_operation_failed",
            "The local database operation failed.",
            command=command,
        )
        return 2
    finally:
        engine.dispose()
    case = ImprovementCaseBuilder().build(snapshot, reported_error_code=reported_error_code)
    _emit({"case": case.model_dump(mode="json"), "command": command, "ok": True})
    return 0


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

    if command_name == "mcp-config":
        return _run_mcp_config(namespace)

    if command_name == "data":
        return _run_data_command(namespace, environment=environment)

    if command_name == "improvement-case":
        return _run_improvement_case(namespace, environment=environment)

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
