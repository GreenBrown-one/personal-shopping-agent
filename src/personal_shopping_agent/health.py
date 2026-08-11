"""Small dependency-free health contract for local and CI smoke tests."""

from dataclasses import dataclass
from typing import Literal

from personal_shopping_agent.__about__ import __version__

SERVICE_NAME = "personal-shopping-agent"


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Serializable health state exposed by future CLI and MCP adapters."""

    service: str
    status: Literal["ok"]
    version: str


def health_check() -> HealthStatus:
    """Return a deterministic process-level health result."""

    return HealthStatus(service=SERVICE_NAME, status="ok", version=__version__)
