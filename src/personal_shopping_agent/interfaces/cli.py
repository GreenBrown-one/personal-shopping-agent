"""Safe local administration CLI: health, migrations, data lifecycle, and improvement cases."""

import argparse
import asyncio
import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast
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
from personal_shopping_agent.infrastructure.benchmark_store import (
    BenchmarkStoreError,
    LocalJsonBenchmarkStore,
)
from personal_shopping_agent.infrastructure.settings import (
    benchmark_file_from_environment,
    database_url_from_environment,
)
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
from personal_shopping_agent.interfaces.composition import (
    create_benchmark_policy,
    create_jd_sign_in_policy,
)
from personal_shopping_agent.interfaces.health import health_check
from personal_shopping_agent.interfaces.host_config import (
    SERVER_NAME,
    ClaudeDesktopConfigError,
    SourceCheckoutError,
    build_source_mcp_configuration,
    claude_desktop_config_path,
    install_claude_desktop_server,
)
from personal_shopping_agent.sourcing.benchmarks import (
    ChipBenchmarkReference,
    chip_name_token,
    suggested_chip_criterion,
)
from personal_shopping_agent.sourcing.browser import (
    BrowserManager,
    BrowserManagerError,
    BrowserManagerSettings,
    ControlledPageCollector,
    NavigationPolicyError,
    StatusPageCollector,
)
from personal_shopping_agent.sourcing.platforms.jd import JD_SIGN_IN_URL
from personal_shopping_agent.sourcing.platforms.socpk import (
    SOCPK_OVERALL_URL,
    BenchmarkPageParseError,
    SocpkRankingParser,
)

SIGN_IN_INSTRUCTIONS = (
    "A visible browser window opened with the dedicated local profile. Sign in to JD yourself "
    "(complete any verification yourself as well), then return here and press Enter to close "
    "the browser. This program never reads or stores your password.\n"
)


class SignInBrowser(Protocol):
    """The browser lifecycle subset a manual sign-in session needs."""

    async def start(self) -> None: ...

    async def open(self, url: str, *, screenshot: bool = False) -> object: ...

    async def close(self) -> None: ...


class BenchmarkBrowser(StatusPageCollector, Protocol):
    """A status-aware page collector with an explicit lifecycle."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...


def create_benchmark_browser() -> BenchmarkBrowser:
    """A headless browser with its own profile, separate from any signed-in shop profile."""

    return BrowserManager(
        create_benchmark_policy(),
        settings=BrowserManagerSettings(
            profile_directory=Path("data/browser-profiles/benchmarks"),
            headless=True,
        ),
    )


def create_sign_in_browser() -> SignInBrowser:
    return BrowserManager(
        create_jd_sign_in_policy(),
        settings=BrowserManagerSettings(headless=False),
    )


def wait_for_enter() -> None:
    sys.stderr.write(SIGN_IN_INSTRUCTIONS)
    sys.stderr.flush()
    sys.stdin.readline()


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
    install_parser = commands.add_parser(
        "install-claude-desktop",
        help="add this project to Claude Desktop's MCP servers, keeping everything else",
    )
    install_parser.add_argument("--project-directory", default=".")
    install_parser.add_argument("--live-jd", action="store_true")
    install_parser.add_argument("--dry-run", action="store_true")
    install_parser.add_argument(
        "--config-path",
        help="Claude Desktop config file; defaults to the documented location for this OS",
    )
    benchmark_parser = commands.add_parser(
        "benchmark",
        help="refresh or show the private local chip performance reference",
    )
    benchmark_commands = benchmark_parser.add_subparsers(dest="benchmark_command", required=True)
    benchmark_commands.add_parser(
        "refresh",
        help="read the public Geekerwan SOCPK ranking page once and save it locally",
    )
    show_parser = benchmark_commands.add_parser("show", help="print the stored chip scores")
    show_parser.add_argument("--filter", help="only chips whose folded name contains this text")
    login_parser = commands.add_parser(
        "login",
        help="sign in yourself in a visible browser using the dedicated local profile",
    )
    login_parser.add_argument("platform", choices=("jd",))
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


def _run_install_claude_desktop(
    namespace: argparse.Namespace,
    *,
    environment: Mapping[str, str] | None,
) -> int:
    command = "install_claude_desktop"
    values = os.environ if environment is None else environment
    live_jd = cast(bool, namespace.live_jd)
    try:
        explicit_path = cast(str | None, namespace.config_path)
        config_path = (
            Path(explicit_path)
            if explicit_path
            else claude_desktop_config_path(values, platform=sys.platform, home=Path.home())
        )
        configuration = build_source_mcp_configuration(
            Path(cast(str, namespace.project_directory)),
            live_jd=live_jd,
            uv_command=values.get("UV") or shutil.which("uv") or "uv",
        )
        servers = cast(dict[str, dict[str, object]], configuration["mcpServers"])
        result = install_claude_desktop_server(
            config_path,
            servers[SERVER_NAME],
            dry_run=cast(bool, namespace.dry_run),
        )
    except SourceCheckoutError:
        _emit_data_error(
            "source_checkout_invalid",
            "Run this from a complete Personal Shopping Agent source checkout.",
            command=command,
        )
        return 2
    except ClaudeDesktopConfigError as error:
        _emit_data_error(error.code, str(error), command=command)
        return 2
    _emit(
        {
            "backup_path": str(result.backup_path) if result.backup_path else None,
            "command": command,
            "config_path": str(result.config_path),
            "live_jd": live_jd,
            "ok": True,
            "server": dict(result.server),
            "status": result.status.value,
            "next_step": (
                "Fully quit and reopen Claude Desktop, then ask it to call shopping_agent_status."
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


async def _collect_benchmark(browser: BenchmarkBrowser) -> ChipBenchmarkReference:
    try:
        await browser.start()
        page = await ControlledPageCollector(browser).open(SOCPK_OVERALL_URL)
        return SocpkRankingParser().parse(page)
    finally:
        await browser.close()


def _reference_summary(reference: ChipBenchmarkReference) -> dict[str, object]:
    return {
        "captured_at": reference.captured_at.isoformat(),
        "chips": len(reference.entries),
        "method": reference.method,
        "source_title": reference.source_title,
        "source_url": str(reference.source_url),
        "suggested_criterion": suggested_chip_criterion(reference),
    }


def _run_benchmark(
    namespace: argparse.Namespace,
    *,
    environment: Mapping[str, str] | None,
    browser_factory: Callable[[], BenchmarkBrowser],
) -> int:
    subcommand = cast(str, namespace.benchmark_command)
    command = f"benchmark_{subcommand}"
    store = LocalJsonBenchmarkStore(Path(benchmark_file_from_environment(environment)))
    if subcommand == "refresh":
        try:
            reference = asyncio.run(_collect_benchmark(browser_factory()))
            store.save(reference)
        except (BrowserManagerError, NavigationPolicyError):
            _emit_data_error(
                "benchmark_page_unavailable",
                "The ranking page could not be opened safely; nothing was saved.",
                command=command,
            )
            return 2
        except BenchmarkPageParseError:
            _emit_data_error(
                "benchmark_page_unrecognized",
                "The ranking page layout was not recognized; nothing was saved.",
                command=command,
            )
            return 2
        except BenchmarkStoreError:
            _emit_data_error(
                "benchmark_store_failed",
                "The private benchmark file could not be written.",
                command=command,
            )
            return 2
        _emit({"command": command, "ok": True, **_reference_summary(reference)})
        return 0

    try:
        reference = store.load()
    except BenchmarkStoreError:
        _emit_data_error(
            "benchmark_unreadable",
            "The private benchmark file is unreadable or not private; refresh it.",
            command=command,
        )
        return 2
    if reference is None:
        _emit_data_error(
            "benchmark_missing",
            "Run `personal-shopping-agent benchmark refresh` first.",
            command=command,
        )
        return 1
    text_filter = cast(str | None, namespace.filter)
    token = chip_name_token(text_filter) if text_filter else ""
    entries = sorted(
        (item for item in reference.entries if token in chip_name_token(item.name)),
        key=lambda item: (-item.score, item.name),
    )
    _emit(
        {
            "command": command,
            "entries": [{"name": item.name, "score": format(item.score, "f")} for item in entries],
            "ok": True,
            **_reference_summary(reference),
        }
    )
    return 0


async def _sign_in_session(browser: SignInBrowser, wait_for_user: Callable[[], None]) -> str:
    try:
        await browser.start()
    except BrowserManagerError:
        return "browser_unavailable"
    try:
        await browser.open(JD_SIGN_IN_URL)
        await asyncio.to_thread(wait_for_user)
    except (BrowserManagerError, NavigationPolicyError):
        return "sign_in_page_unavailable"
    finally:
        await browser.close()
    return ""


def _run_login(
    namespace: argparse.Namespace,
    *,
    browser_factory: Callable[[], SignInBrowser],
    wait_for_user: Callable[[], None],
) -> int:
    platform = cast(str, namespace.platform)
    error_code = asyncio.run(_sign_in_session(browser_factory(), wait_for_user))
    if error_code == "browser_unavailable":
        _emit_data_error(
            error_code,
            "Chromium could not start. Run `uv run --locked playwright install chromium` and "
            "make sure data/browser-profiles is private to your account.",
            command="login",
        )
        return 2
    if error_code:
        _emit_data_error(
            error_code,
            "The sign-in page could not be opened safely; the browser was closed.",
            command="login",
        )
        return 2
    _emit(
        {
            "command": "login",
            "message": "Browser closed. The sign-in state stays only in the private local profile.",
            "ok": True,
            "platform": platform,
        }
    )
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    sign_in_browser_factory: Callable[[], SignInBrowser] = create_sign_in_browser,
    wait_for_user: Callable[[], None] = wait_for_enter,
    benchmark_browser_factory: Callable[[], BenchmarkBrowser] = create_benchmark_browser,
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

    if command_name == "install-claude-desktop":
        return _run_install_claude_desktop(namespace, environment=environment)

    if command_name == "improvement-case":
        return _run_improvement_case(namespace, environment=environment)

    if command_name == "benchmark":
        return _run_benchmark(
            namespace,
            environment=environment,
            browser_factory=benchmark_browser_factory,
        )

    if command_name == "login":
        return _run_login(
            namespace,
            browser_factory=sign_in_browser_factory,
            wait_for_user=wait_for_user,
        )

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
