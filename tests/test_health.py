"""Tests for the initial package health contract."""

import json

import pytest

from personal_shopping_agent import HealthStatus, health_check
from personal_shopping_agent.__main__ import main


def test_health_check_is_stable() -> None:
    result = health_check()

    assert result == HealthStatus(
        service="personal-shopping-agent",
        status="ok",
        version="0.1.0",
    )


def test_main_prints_health_json(capsys: pytest.CaptureFixture[str]) -> None:
    main()

    assert json.loads(capsys.readouterr().out) == {
        "service": "personal-shopping-agent",
        "status": "ok",
        "version": "0.1.0",
    }
