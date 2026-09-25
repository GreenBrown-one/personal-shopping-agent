"""Source-checkout bootstrap process tests."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from personal_shopping_agent.infrastructure.settings import DATABASE_URL_ENV

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SCRIPT = PROJECT_ROOT / "scripts" / "bootstrap.py"


def _run_bootstrap(database_url: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment[DATABASE_URL_ENV] = database_url
    environment["PERSONAL_SHOPPING_LLM_PROVIDER"] = "disabled"
    return subprocess.run(
        (sys.executable, str(BOOTSTRAP_SCRIPT)),
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _payloads(output: str) -> list[dict[str, object]]:
    values = [json.loads(line) for line in output.splitlines() if line]
    assert all(isinstance(value, dict) for value in values)
    return cast(list[dict[str, object]], values)


def test_bootstrap_initializes_and_verifies_a_private_database(tmp_path: Path) -> None:
    database_path = tmp_path / "state" / "shopping.db"

    completed = _run_bootstrap(f"sqlite:///{database_path}")

    assert completed.returncode == 0, completed.stderr
    payloads = _payloads(completed.stdout)
    assert [payload.get("command") for payload in payloads] == [
        None,
        "migrate",
        "doctor",
        "bootstrap",
    ]
    assert payloads[-1] == {
        "command": "bootstrap",
        "ok": True,
        "service": "personal-shopping-agent",
    }
    assert database_path.is_file()


def test_bootstrap_stops_before_completion_for_an_invalid_database() -> None:
    completed = _run_bootstrap("postgresql://localhost/shopping")

    assert completed.returncode == 2
    payloads = _payloads(completed.stdout)
    assert [payload.get("command") for payload in payloads] == [None, "migrate"]
    assert payloads[-1]["error_code"] == "invalid_database_url"


def _bootstrap_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location("bootstrap_script", BOOTSTRAP_SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_bootstrap_flags_append_the_claude_desktop_installer_last() -> None:
    commands = _bootstrap_module().bootstrap_commands

    assert commands([]) == (("health",), ("migrate",), ("doctor",))
    assert commands(["--claude-desktop"])[-1] == ("install-claude-desktop",)
    assert commands(["--claude-desktop", "--live-jd"])[-1] == (
        "install-claude-desktop",
        "--live-jd",
    )
    with pytest.raises(SystemExit):
        commands(["--live-jd"])


def test_windows_setup_script_keeps_crlf_and_calls_only_public_commands() -> None:
    script = (PROJECT_ROOT / "install-windows.cmd").read_bytes()

    assert script.isascii()
    assert b"\r\n" in script and b"\n" not in script.replace(b"\r\n", b"")
    text = script.decode("ascii")
    invoked = [line.strip() for line in text.splitlines() if line.strip().startswith("uv run")]
    assert invoked == [
        r"uv run --locked python scripts\bootstrap.py --claude-desktop %LIVE%",
        "uv run --locked playwright install chromium",
        "uv run --locked personal-shopping-agent login jd",
        "uv run --locked personal-shopping-agent benchmark refresh",
    ]
    assert "https://astral.sh/uv/install.ps1" in text
    assert (PROJECT_ROOT / ".gitattributes").read_text().splitlines()[-1] == "*.cmd -text"
