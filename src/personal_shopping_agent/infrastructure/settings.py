"""Small process-level settings shared by CLI and MCP entry points."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "sqlite:///data/personal-shopping-agent.db"
DATABASE_URL_ENV = "PERSONAL_SHOPPING_DATABASE_URL"
LIVE_JD_ACCESS_ENV = "PERSONAL_SHOPPING_ENABLE_LIVE_JD"
LIVE_JD_HEADLESS_ENV = "PERSONAL_SHOPPING_JD_HEADLESS"
BENCHMARK_FILE_ENV = "PERSONAL_SHOPPING_BENCHMARK_FILE"
INBOX_DIR_ENV = "PERSONAL_SHOPPING_INBOX_DIR"
DEFAULT_INBOX_DIR = "data/inbox"
DEFAULT_BENCHMARK_FILE = "data/benchmarks/socpk-allperf.json"


class LiveJDConfigurationError(ValueError):
    """Stable, sanitized error for explicit live-JD process settings."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LiveJDSettings:
    """Minimal live-platform settings; access remains disabled by default."""

    enabled: bool = False
    headless: bool = True


def database_url_from_environment(environment: Mapping[str, str] | None = None) -> str:
    """Read the local database URL without retaining a mutable environment mapping."""

    values = os.environ if environment is None else environment
    return values.get(DATABASE_URL_ENV, DEFAULT_DATABASE_URL)


def benchmark_file_from_environment(environment: Mapping[str, str] | None = None) -> str:
    """Read the private local chip benchmark reference path."""

    values = os.environ if environment is None else environment
    return values.get(BENCHMARK_FILE_ENV, DEFAULT_BENCHMARK_FILE)


def inbox_directory_from_environment(environment: Mapping[str, str] | None = None) -> str:
    """Read the private directory the user saves browser pages into."""

    values = os.environ if environment is None else environment
    return values.get(INBOX_DIR_ENV, DEFAULT_INBOX_DIR)


def load_live_jd_settings(environment: Mapping[str, str] | None = None) -> LiveJDSettings:
    """Read strict booleans without enabling live access through a truthy typo."""

    values = os.environ if environment is None else environment
    return LiveJDSettings(
        enabled=_strict_boolean(values, LIVE_JD_ACCESS_ENV, default=False),
        headless=_strict_boolean(values, LIVE_JD_HEADLESS_ENV, default=True),
    )


def _strict_boolean(values: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw_value = values.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise LiveJDConfigurationError(
        "invalid_boolean",
        f"{name} must be exactly true or false.",
    )
