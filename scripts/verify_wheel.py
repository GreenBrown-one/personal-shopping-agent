"""Verify wheel metadata, resources, entry points, and a clean installation."""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from zipfile import ZipFile

from personal_shopping_agent.__about__ import __version__

_PROJECT_NAME = "personal-shopping-agent"
_EXPECTED_CONSOLE_SCRIPTS = {
    "personal-shopping-agent": "personal_shopping_agent.cli:main",
    "personal-shopping-agent-mcp": "personal_shopping_agent.mcp.server:main",
}
_REQUIRED_MEMBERS = {
    "personal_shopping_agent/__about__.py",
    "personal_shopping_agent/migrations/env.py",
    "personal_shopping_agent/migrations/script.py.mako",
    "personal_shopping_agent/migrations/versions/20260809_0001_initial_domain_storage.py",
    "personal_shopping_agent/migrations/versions/20260809_0002_workflow_orchestration.py",
    "personal_shopping_agent/migrations/versions/20260809_0003_platform_observations.py",
    "personal_shopping_agent/migrations/versions/20260809_0004_evidence_cross_checks.py",
    "personal_shopping_agent/migrations/versions/20260809_0005_normalized_specifications.py",
    "personal_shopping_agent/migrations/versions/20260809_0006_candidate_scores.py",
    "personal_shopping_agent/migrations/versions/20260809_0007_shopping_reports.py",
    "personal_shopping_agent/templates/shopping_report.html.j2",
}


def _resolve_wheel(argument: str) -> Path:
    candidate = Path(argument).resolve()
    if candidate.is_file():
        wheels = [candidate]
    elif candidate.is_dir():
        wheels = sorted(candidate.glob("*.whl"))
    else:
        raise SystemExit(f"wheel path does not exist: {candidate}")
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel, found {len(wheels)} in {candidate}")
    return wheels[0]


def _single_member(members: set[str], suffix: str) -> str:
    matches = sorted(member for member in members if member.endswith(suffix))
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one {suffix} member, found {len(matches)}")
    return matches[0]


def _metadata_field(metadata: str, field: str) -> str:
    prefix = f"{field}: "
    matches = [
        line.removeprefix(prefix) for line in metadata.splitlines() if line.startswith(prefix)
    ]
    if len(matches) != 1 or not matches[0]:
        raise SystemExit(f"wheel metadata must contain exactly one {field} field")
    return matches[0]


def _inspect_wheel(wheel: Path) -> str:
    try:
        with ZipFile(wheel) as archive:
            members = set(archive.namelist())
            missing = sorted(_REQUIRED_MEMBERS - members)
            if missing:
                raise SystemExit(f"wheel is missing packaged resources: {', '.join(missing)}")

            metadata_member = _single_member(members, ".dist-info/METADATA")
            metadata = archive.read(metadata_member).decode("utf-8")
            project_name = _metadata_field(metadata, "Name")
            wheel_version = _metadata_field(metadata, "Version")
            if project_name != _PROJECT_NAME:
                raise SystemExit(
                    f"unexpected wheel project name: expected {_PROJECT_NAME}, got {project_name}"
                )
            if wheel_version != __version__:
                raise SystemExit(
                    "wheel version does not match the runtime version: "
                    f"{wheel_version} != {__version__}"
                )

            entry_points_member = _single_member(members, ".dist-info/entry_points.txt")
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(archive.read(entry_points_member).decode("utf-8"))
            if "console_scripts" not in parser:
                raise SystemExit("wheel does not define a console_scripts entry-point group")
            actual_scripts = dict(parser["console_scripts"].items())
            if actual_scripts != _EXPECTED_CONSOLE_SCRIPTS:
                raise SystemExit(
                    f"wheel console scripts do not match the release contract: {actual_scripts!r}"
                )
    except OSError as error:
        raise SystemExit(f"wheel could not be inspected: {error}") from error
    return wheel_version


def _clean_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "UV_ACTIVE",
        "UV_PROJECT_ENVIRONMENT",
    ):
        environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PERSONAL_SHOPPING_LLM_PROVIDER"] = "disabled"
    return environment


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    expected_return_codes: frozenset[int] = frozenset({0}),
    timeout_seconds: int = 300,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode not in expected_return_codes:
        rendered_command = " ".join(command)
        raise SystemExit(
            f"command failed with exit code {completed.returncode}: {rendered_command}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _json_object(output: str, label: str) -> dict[str, object]:
    try:
        value: object = json.loads(output)
    except json.JSONDecodeError as error:
        raise SystemExit(f"{label} did not emit valid JSON: {output!r}") from error
    return _object_mapping(value, label)


def _object_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must emit one JSON object")
    return cast(dict[str, object], value)


def _require_database_state(
    payload: Mapping[str, object],
    *,
    exists: bool,
    ready: bool,
    label: str,
) -> None:
    database = _object_mapping(payload.get("database"), f"{label} database state")
    if database.get("database_exists") is not exists or database.get("ready") is not ready:
        raise SystemExit(f"{label} returned an unexpected database state: {database!r}")
    if ready and database.get("current_revision") != database.get("target_revision"):
        raise SystemExit(f"{label} did not reach the packaged migration head")
    expected_permissions = True if exists and os.name == "posix" else None
    if database.get("private_file_permissions") is not expected_permissions:
        raise SystemExit(f"{label} returned an unexpected private-file permission state")


def _verify_clean_install(wheel: Path, wheel_version: str) -> None:
    uv_executable = shutil.which("uv")
    if uv_executable is None:
        raise SystemExit("uv is required for the clean-install wheel verification")

    with tempfile.TemporaryDirectory(prefix="shopping-agent-wheel-") as temporary:
        temporary_path = Path(temporary)
        environment = _clean_environment()
        virtual_environment = temporary_path / "venv"
        _run(
            (uv_executable, "venv", "--python", sys.executable, str(virtual_environment)),
            cwd=temporary_path,
            environment=environment,
        )

        executable_directory = virtual_environment / ("Scripts" if os.name == "nt" else "bin")
        python_executable = executable_directory / ("python.exe" if os.name == "nt" else "python")
        cli_executable = executable_directory / (
            "personal-shopping-agent.exe" if os.name == "nt" else "personal-shopping-agent"
        )
        mcp_cli_executable = executable_directory / (
            "personal-shopping-agent-mcp.exe" if os.name == "nt" else "personal-shopping-agent-mcp"
        )
        _run(
            (
                uv_executable,
                "pip",
                "install",
                "--python",
                str(python_executable),
                str(wheel),
            ),
            cwd=temporary_path,
            environment=environment,
        )
        missing_executables = [
            executable.name
            for executable in (cli_executable, mcp_cli_executable)
            if not executable.is_file()
        ]
        if missing_executables:
            raise SystemExit(
                f"the installed wheel did not create: {', '.join(missing_executables)}"
            )

        health = _json_object(
            _run(
                (str(cli_executable), "health"),
                cwd=temporary_path,
                environment=environment,
            ).stdout,
            "installed health command",
        )
        if health != {
            "service": _PROJECT_NAME,
            "status": "ok",
            "version": wheel_version,
        }:
            raise SystemExit(f"installed health contract is unexpected: {health!r}")

        database_path = temporary_path / "state" / "installed.db"
        database_url = f"sqlite:///{database_path}"
        doctor_before = _json_object(
            _run(
                (str(cli_executable), "doctor", "--database-url", database_url),
                cwd=temporary_path,
                environment=environment,
                expected_return_codes=frozenset({1}),
            ).stdout,
            "installed doctor command before migration",
        )
        if doctor_before.get("command") != "doctor" or doctor_before.get("ok") is not False:
            raise SystemExit(f"doctor did not fail closed before migration: {doctor_before!r}")
        _require_database_state(doctor_before, exists=False, ready=False, label="initial doctor")

        migrated = _json_object(
            _run(
                (str(cli_executable), "migrate", "--database-url", database_url),
                cwd=temporary_path,
                environment=environment,
            ).stdout,
            "installed migrate command",
        )
        if migrated.get("command") != "migrate" or migrated.get("ok") is not True:
            raise SystemExit(f"migrate did not report success: {migrated!r}")
        _require_database_state(migrated, exists=True, ready=True, label="migrate")

        doctor_after = _json_object(
            _run(
                (str(cli_executable), "doctor", "--database-url", database_url),
                cwd=temporary_path,
                environment=environment,
            ).stdout,
            "installed doctor command after migration",
        )
        if doctor_after.get("command") != "doctor" or doctor_after.get("ok") is not True:
            raise SystemExit(f"doctor did not report success after migration: {doctor_after!r}")
        _require_database_state(doctor_after, exists=True, ready=True, label="final doctor")

        mcp_environment = dict(environment)
        mcp_environment["PERSONAL_SHOPPING_DATABASE_URL"] = database_url
        composition_code = "\n".join(
            (
                "import json",
                "import sys",
                "from pathlib import Path",
                "import personal_shopping_agent as package",
                "from personal_shopping_agent.mcp.server import create_default_server",
                "server = create_default_server()",
                "package_path = Path(package.__file__).resolve()",
                "assert package_path.is_relative_to(Path(sys.prefix).resolve())",
                f"assert package.__version__ == {wheel_version!r}",
                "assert server.name == 'personal-shopping-agent'",
                "assert server.version == package.__version__",
                "print(json.dumps({'package': str(package_path), 'version': server.version}))",
            )
        )
        _json_object(
            _run(
                (str(python_executable), "-c", composition_code),
                cwd=temporary_path,
                environment=mcp_environment,
            ).stdout,
            "installed MCP composition check",
        )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: verify_wheel.py PATH_TO_WHEEL_OR_DIST_DIRECTORY")
    wheel = _resolve_wheel(argv[0])
    wheel_version = _inspect_wheel(wheel)
    _verify_clean_install(wheel, wheel_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
