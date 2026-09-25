"""Secret-free local MCP host configuration generation."""

from pathlib import Path

from personal_shopping_agent.infrastructure.settings import LIVE_JD_ACCESS_ENV, LIVE_JD_HEADLESS_ENV


class SourceCheckoutError(ValueError):
    """Sanitized failure for a missing or incomplete source checkout."""


def build_source_mcp_configuration(
    project_directory: Path,
    *,
    live_jd: bool = False,
) -> dict[str, object]:
    """Build a generic stdio MCP block without reading or embedding secrets."""

    try:
        resolved = project_directory.expanduser().resolve(strict=True)
    except OSError as error:
        raise SourceCheckoutError("The project directory does not exist.") from error
    required_files = (resolved / "pyproject.toml", resolved / "uv.lock")
    package_directory = resolved / "src" / "personal_shopping_agent"
    if not resolved.is_dir() or not all(path.is_file() for path in required_files):
        raise SourceCheckoutError("The project directory is not a complete source checkout.")
    if not package_directory.is_dir():
        raise SourceCheckoutError("The project directory is not a complete source checkout.")

    entrypoint = "personal-shopping-agent-mcp-jd" if live_jd else "personal-shopping-agent-mcp"
    environment = {"PERSONAL_SHOPPING_LLM_PROVIDER": "disabled"}
    if live_jd:
        environment[LIVE_JD_ACCESS_ENV] = "true"
        # A visible window lets the user watch every page and step in at any verification prompt.
        environment[LIVE_JD_HEADLESS_ENV] = "false"
    return {
        "mcpServers": {
            "personal-shopping-agent": {
                "command": "uv",
                "args": [
                    "--directory",
                    str(resolved),
                    "run",
                    "--locked",
                    entrypoint,
                ],
                "env": environment,
            }
        }
    }
