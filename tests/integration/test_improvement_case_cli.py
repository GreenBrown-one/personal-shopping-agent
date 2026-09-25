"""The improvement-case CLI prints a sanitized local preview and never writes anything."""

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from personal_shopping_agent.automation import ShoppingWorkflowService
from personal_shopping_agent.domain import Budget, Money, ShoppingRequest, WorkflowState
from personal_shopping_agent.infrastructure.storage import (
    SQLiteWorkflowRepository,
    create_session_factory,
    create_sqlite_engine,
    upgrade_database,
)
from personal_shopping_agent.interfaces.cli import main

PRIVATE_QUERY = "送到昆明市五华区的手机"


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def _seed(tmp_path: Path) -> tuple[str, str]:
    database_url = f"sqlite:///{tmp_path / 'shopping.db'}"
    assert upgrade_database(database_url).ready is True
    engine = create_sqlite_engine(database_url)
    snapshot = ShoppingWorkflowService(
        SQLiteWorkflowRepository(create_session_factory(engine))
    ).start(
        ShoppingRequest(
            query=PRIVATE_QUERY,
            category="smartphone",
            budget=Budget(maximum=Money(amount=Decimal("3000"))),
        )
    )
    engine.dispose()
    return database_url, str(snapshot.workflow.id)


def test_cli_prints_a_sanitized_preview_without_writing_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url, workflow_id = _seed(tmp_path)
    before = sorted(path.name for path in tmp_path.iterdir())

    exit_code = main(
        [
            "improvement-case",
            workflow_id,
            "--error-code",
            "platform_access_restricted",
            "--database-url",
            database_url,
        ],
        environment={},
    )

    raw = capsys.readouterr().out
    assert exit_code == 0
    assert PRIVATE_QUERY not in raw
    assert workflow_id not in raw
    payload = json.loads(raw)
    assert payload["ok"] is True
    case = payload["case"]
    assert case["outcome"] == "stalled"
    assert case["stage"] == WorkflowState.CANDIDATES_DISCOVERED.value
    assert case["stable_error_code"] == "platform_access_restricted"
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_cli_reports_sanitized_input_and_database_failures(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url, workflow_id = _seed(tmp_path)

    assert main(["improvement-case", "not-a-uuid"], environment={}) == 2
    assert _output(capsys)["error_code"] == "invalid_workflow_id"

    arguments = ["improvement-case", workflow_id, "--database-url", database_url]
    assert main([*arguments, "--error-code", "Free Text"], environment={}) == 2
    assert _output(capsys)["error_code"] == "invalid_error_code"

    unknown = ["improvement-case", str(uuid4()), "--database-url", database_url]
    assert main(unknown, environment={}) == 1
    assert _output(capsys)["error_code"] == "workflow_not_found"

    postgres = ["improvement-case", workflow_id, "--database-url", "postgresql://localhost/x"]
    assert main(postgres, environment={}) == 2
    assert _output(capsys)["error_code"] == "invalid_database_url"

    missing = tmp_path / "missing" / "shopping.db"
    assert (
        main(
            ["improvement-case", workflow_id, "--database-url", f"sqlite:///{missing}"],
            environment={},
        )
        == 1
    )
    assert _output(capsys)["error_code"] == "database_not_ready"
    assert not missing.exists()

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    corrupt.chmod(0o600)
    assert (
        main(
            ["improvement-case", workflow_id, "--database-url", f"sqlite:///{corrupt}"],
            environment={},
        )
        == 2
    )
    assert _output(capsys)["error_code"] == "database_operation_failed"

    def fail_read(*_: object, **__: object) -> None:
        raise OperationalError("SELECT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(SQLiteWorkflowRepository, "get_snapshot", fail_read)
    assert main(arguments, environment={}) == 2
    failure = _output(capsys)
    assert failure["error_code"] == "database_operation_failed"
    assert "disk" not in json.dumps(failure)
