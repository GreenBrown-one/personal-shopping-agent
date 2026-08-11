"""Verify that a built wheel can migrate a fresh database without the source tree."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

_REQUIRED_MEMBERS = {
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


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: verify_wheel.py PATH_TO_WHEEL")
    wheel = Path(argv[0]).resolve()
    with tempfile.TemporaryDirectory(prefix="shopping-agent-wheel-") as temporary:
        extraction = Path(temporary) / "installed"
        extraction.mkdir()
        with ZipFile(wheel) as archive:
            members = set(archive.namelist())
            missing = sorted(_REQUIRED_MEMBERS - members)
            if missing:
                raise SystemExit(f"wheel is missing packaged resources: {', '.join(missing)}")
            archive.extractall(extraction)

        database = Path(temporary) / "state" / "wheel.db"
        code = "\n".join(
            (
                "import sys",
                f"sys.path.insert(0, {str(extraction)!r})",
                "from personal_shopping_agent.cli import main",
                f"url = {'sqlite:///' + str(database)!r}",
                "assert main(['migrate', '--database-url', url], environment={}) == 0",
                "assert main(['doctor', '--database-url', url], environment={}) == 0",
            )
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=temporary,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise SystemExit(
                "built-wheel migration smoke test failed:\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
