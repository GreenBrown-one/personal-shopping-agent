"""Secret-free local MCP host configuration generation and Claude Desktop installation."""

import json
import os
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from personal_shopping_agent.infrastructure.settings import LIVE_JD_ACCESS_ENV, LIVE_JD_HEADLESS_ENV


class SourceCheckoutError(ValueError):
    """Sanitized failure for a missing or incomplete source checkout."""


def build_source_mcp_configuration(
    project_directory: Path,
    *,
    live_jd: bool = False,
    uv_command: str = "uv",
) -> dict[str, object]:
    """Build a generic stdio MCP block without reading or embedding secrets."""

    try:
        resolved = project_directory.expanduser().resolve(strict=True)
    except OSError as error:
        raise SourceCheckoutError("The project directory does not exist.") from error
    required_files = (resolved / "pyproject.toml", resolved / "uv.lock")
    package_directory = resolved / "src" / "personal_shopping_agent"
    if not resolved.is_dir() or not all(path.is_file() for path in required_files):
        raise SourceCheckoutError("The project directory is not a complete source checkout.")
    if not package_directory.is_dir():
        raise SourceCheckoutError("The project directory is not a complete source checkout.")

    entrypoint = "personal-shopping-agent-mcp-jd" if live_jd else "personal-shopping-agent-mcp"
    environment = {"PERSONAL_SHOPPING_LLM_PROVIDER": "disabled"}
    if live_jd:
        environment[LIVE_JD_ACCESS_ENV] = "true"
        # A visible window lets the user watch every page and step in at any verification prompt.
        environment[LIVE_JD_HEADLESS_ENV] = "false"
    return {
        "mcpServers": {
            "personal-shopping-agent": {
                "command": uv_command,
                "args": [
                    "--directory",
                    str(resolved),
                    "run",
                    "--locked",
                    entrypoint,
                ],
                "env": environment,
            }
        }
    }


SERVER_NAME = "personal-shopping-agent"


class ClaudeDesktopConfigError(ValueError):
    """Sanitized reason the Claude Desktop configuration was left untouched."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InstallStatus(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    PREVIEW = "preview"


@dataclass(frozen=True, slots=True)
class ClaudeDesktopInstallResult:
    """Only this project's entry is reported; other servers may hold secrets."""

    status: InstallStatus
    config_path: Path
    backup_path: Path | None
    server: Mapping[str, object]


def claude_desktop_config_path(
    environment: Mapping[str, str],
    *,
    platform: str,
    home: Path,
) -> Path:
    """Return the documented Claude Desktop configuration file for this operating system."""

    if platform == "win32":
        application_data = environment.get("APPDATA")
        if not application_data:
            raise ClaudeDesktopConfigError("appdata_missing", "APPDATA is not set.")
        return Path(application_data) / "Claude" / "claude_desktop_config.json"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    raise ClaudeDesktopConfigError(
        "unsupported_platform",
        "Claude Desktop runs on Windows and macOS; pass --config-path explicitly elsewhere.",
    )


def install_claude_desktop_server(
    config_path: Path,
    server: Mapping[str, object],
    *,
    dry_run: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ClaudeDesktopInstallResult:
    """Merge one server entry, keep everything else, and back up before any change."""

    try:
        original = config_path.read_bytes()
    except FileNotFoundError:
        original = None
    except OSError as error:
        raise ClaudeDesktopConfigError(
            "config_unreadable", "The config could not be read."
        ) from error

    document: dict[str, object] = {}
    if original is not None:
        try:
            loaded: object = json.loads(original.decode("utf-8-sig") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ClaudeDesktopConfigError(
                "config_invalid", "The existing config is not valid JSON; it was left untouched."
            ) from error
        if not isinstance(loaded, dict):
            raise ClaudeDesktopConfigError(
                "config_invalid", "The existing config is not a JSON object; it was left untouched."
            )
        document = cast(dict[str, object], loaded)
    servers_value = document.get("mcpServers", {})
    if not isinstance(servers_value, dict):
        raise ClaudeDesktopConfigError(
            "config_invalid", "mcpServers is not a JSON object; the config was left untouched."
        )
    servers = cast(dict[str, object], servers_value)

    if servers.get(SERVER_NAME) == server:
        status = InstallStatus.UNCHANGED
    elif dry_run:
        status = InstallStatus.PREVIEW
    else:
        status = InstallStatus.CREATED if original is None else InstallStatus.UPDATED
    if status in {InstallStatus.UNCHANGED, InstallStatus.PREVIEW}:
        return ClaudeDesktopInstallResult(status, config_path, None, server)

    document["mcpServers"] = {**servers, SERVER_NAME: dict(server)}
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    backup_path: Path | None = None
    temporary = config_path.with_name(f".{config_path.name}.tmp")
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if original is not None:
            stamp = clock().strftime("%Y%m%dT%H%M%S")
            backup_path = config_path.with_name(f"{config_path.name}.backup-{stamp}")
            with backup_path.open("xb") as backup:
                backup.write(original)
        with temporary.open("wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, config_path)
    except OSError as error:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise ClaudeDesktopConfigError(
            "config_write_failed", "The config could not be written; the original is unchanged."
        ) from error
    return ClaudeDesktopInstallResult(status, config_path, backup_path, server)
