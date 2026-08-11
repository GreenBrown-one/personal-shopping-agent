"""Tests for the initial package health contract."""

import json
from importlib.metadata import version

import pytest

from personal_shopping_agent import HealthStatus, __version__, health_check
from personal_shopping_agent.__main__ import main


def test_health_check_is_stable() -> None:
    result = health_check()

    assert result == HealthStatus(
        service="personal-shopping-agent",
        status="ok",
        version=__version__,
    )


def test_runtime_version_matches_distribution_metadata() -> None:
    assert version("personal-shopping-agent") == __version__


def test_main_prints_health_json(capsys: pytest.CaptureFixture[str]) -> None:
    main()

    assert json.loads(capsys.readouterr().out) == {
        "service": "personal-shopping-agent",
        "status": "ok",
        "version": __version__,
    }
