"""Secret-free generic MCP host configuration tests."""

from pathlib import Path
from typing import cast

import pytest

from personal_shopping_agent.infrastructure.settings import LIVE_JD_ACCESS_ENV, LIVE_JD_HEADLESS_ENV
from personal_shopping_agent.interfaces.composition import (
    create_jd_collection_policy,
    create_jd_sign_in_policy,
)
from personal_shopping_agent.interfaces.host_config import (
    SourceCheckoutError,
    build_source_mcp_configuration,
)


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
        LIVE_JD_HEADLESS_ENV: "false",
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


def test_jd_policies_separate_collection_pages_from_the_sign_in_flow() -> None:
    collection = create_jd_collection_policy()
    sign_in = create_jd_sign_in_policy()

    assert collection.allowed_hosts == {"search.jd.com", "item.jd.com"}
    assert sign_in.allowed_hosts == {
        "passport.jd.com",
        "www.jd.com",
        "search.jd.com",
        "item.jd.com",
    }
