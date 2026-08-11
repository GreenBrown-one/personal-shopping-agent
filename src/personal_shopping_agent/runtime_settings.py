"""Small process-level settings shared by CLI and MCP entry points."""

import os
from collections.abc import Mapping

DEFAULT_DATABASE_URL = "sqlite:///data/personal-shopping-agent.db"
DATABASE_URL_ENV = "PERSONAL_SHOPPING_DATABASE_URL"


def database_url_from_environment(environment: Mapping[str, str] | None = None) -> str:
    """Read the local database URL without retaining a mutable environment mapping."""

    values = os.environ if environment is None else environment
    return values.get(DATABASE_URL_ENV, DEFAULT_DATABASE_URL)
