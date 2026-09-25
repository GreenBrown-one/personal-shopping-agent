"""Explicit live-JD composition and managed browser lifecycle tests."""

import asyncio
from pathlib import Path

import pytest
from mcp import Client

from personal_shopping_agent.infrastructure.settings import (
    DATABASE_URL_ENV,
    LIVE_JD_ACCESS_ENV,
    LIVE_JD_HEADLESS_ENV,
    LiveJDConfigurationError,
)
from personal_shopping_agent.infrastructure.storage import upgrade_database
from personal_shopping_agent.interfaces.mcp import (
    create_live_jd_server,
    create_live_jd_server_for_database,
)
from personal_shopping_agent.sourcing.browser import StatusPage


class FakeManagedCollector:
    """Lifecycle test double; status calls must not navigate."""

    def __init__(self) -> None:
        self.started = 0
        self.closed = 0

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1

    async def open(self, url: str, *, screenshot: bool = False) -> StatusPage:
        del url, screenshot
        raise AssertionError("capability inspection must not navigate")


def test_live_jd_server_requires_explicit_enablement() -> None:
    with pytest.raises(LiveJDConfigurationError) as captured:
        create_live_jd_server_for_database("sqlite://", environment={})

    assert captured.value.code == "live_jd_disabled"


def test_live_jd_server_manages_the_collector_and_reports_honest_capabilities() -> None:
    collector = FakeManagedCollector()
    server = create_live_jd_server_for_database(
        "sqlite://",
        environment={LIVE_JD_ACCESS_ENV: "true"},
        managed_collector=collector,
    )

    async def scenario() -> None:
        async with Client(server, raise_exceptions=True) as client:
            assert collector.started == 1
            result = await client.call_tool("shopping_agent_status", {})
            assert result.structured_content is not None
            assert result.structured_content["milestone"] == "M8"
            assert result.structured_content["end_to_end_pipeline"] is True
            assert result.structured_content["platform_collection"] is True
            assert result.structured_content["automatic_purchase"] is False
            assert "manual acceptance" in result.structured_content["message"]
        assert collector.closed == 1

    asyncio.run(scenario())


def test_live_jd_factories_cover_production_defaults_and_schema_gate(tmp_path: Path) -> None:
    production_server = create_live_jd_server_for_database(
        "sqlite://",
        environment={
            LIVE_JD_ACCESS_ENV: "true",
            LIVE_JD_HEADLESS_ENV: "false",
        },
    )
    assert production_server.name == "personal-shopping-agent"

    database_path = tmp_path / "live" / "shopping.db"
    database_url = f"sqlite:///{database_path}"
    upgrade_database(database_url)
    collector = FakeManagedCollector()
    gated_server = create_live_jd_server(
        {
            DATABASE_URL_ENV: database_url,
            LIVE_JD_ACCESS_ENV: "true",
        },
        managed_collector=collector,
    )
    assert gated_server.name == "personal-shopping-agent"
