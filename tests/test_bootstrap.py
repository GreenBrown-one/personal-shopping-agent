"""Source-checkout bootstrap process tests."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

from personal_shopping_agent.runtime_settings import DATABASE_URL_ENV

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
