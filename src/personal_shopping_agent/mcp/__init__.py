"""MCP interface package."""

from personal_shopping_agent.mcp.schemas import (
    AgentCapabilities,
    CriterionInput,
    StartShoppingWorkflowInput,
)
from personal_shopping_agent.mcp.server import create_mcp_server, create_server_for_database

__all__ = [
    "AgentCapabilities",
    "CriterionInput",
    "StartShoppingWorkflowInput",
    "create_mcp_server",
    "create_server_for_database",
]
