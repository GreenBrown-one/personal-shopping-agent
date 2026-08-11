"""Secret-free generic MCP host configuration tests."""

from pathlib import Path
from typing import cast

import pytest

from personal_shopping_agent.host_config import (
    SourceCheckoutError,
    build_source_mcp_configuration,
)
from personal_shopping_agent.runtime_settings import LIVE_JD_ACCESS_ENV


def _checkout(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='personal-shopping-agent'\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")
    (tmp_path / "src" / "personal_shopping_agent").mkdir(parents=True)
    return tmp_path.resolve()


def test_default_source_configuration_is_locked_offline_and_secret_free(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)

    configuration = build_source_mcp_configuration(checkout)

    server = cast(dict[str, object], configuration["mcpServers"])
    details = cast(dict[str, object], server["personal-shopping-agent"])
    assert details == {
        "command": "uv",
        "args": [
            "--directory",
            str(checkout),
            "run",
            "--locked",
            "personal-shopping-agent-mcp",
        ],
        "env": {"PERSONAL_SHOPPING_LLM_PROVIDER": "disabled"},
    }


def test_live_source_configuration_selects_only_the_explicit_entrypoint(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)

    configuration = build_source_mcp_configuration(checkout, live_jd=True)

    servers = cast(dict[str, object], configuration["mcpServers"])
    details = cast(dict[str, object], servers["personal-shopping-agent"])
    args = cast(list[str], details["args"])
    assert args[-1] == "personal-shopping-agent-mcp-jd"
    assert cast(dict[str, str], details["env"]) == {
        "PERSONAL_SHOPPING_LLM_PROVIDER": "disabled",
        LIVE_JD_ACCESS_ENV: "true",
    }


def test_source_configuration_rejects_missing_and_incomplete_checkouts(tmp_path: Path) -> None:
    with pytest.raises(SourceCheckoutError, match="does not exist"):
        build_source_mcp_configuration(tmp_path / "missing")

    with pytest.raises(SourceCheckoutError, match="complete source checkout"):
        build_source_mcp_configuration(tmp_path)

    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with pytest.raises(SourceCheckoutError, match="complete source checkout"):
        build_source_mcp_configuration(tmp_path)
