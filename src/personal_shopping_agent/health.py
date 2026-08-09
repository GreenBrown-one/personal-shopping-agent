"""Small dependency-free health contract for local and CI smoke tests."""

from dataclasses import dataclass
from typing import Literal

SERVICE_NAME = "personal-shopping-agent"
SERVICE_VERSION = "0.1.0"


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Serializable health state exposed by future CLI and MCP adapters."""

    service: str
    status: Literal["ok"]
    version: str


def health_check() -> HealthStatus:
    """Return a deterministic process-level health result."""

    return HealthStatus(service=SERVICE_NAME, status="ok", version=SERVICE_VERSION)
