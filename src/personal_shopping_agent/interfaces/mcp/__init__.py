"""MCP interface package."""

from personal_shopping_agent.mcp.schemas import (
    AgentCapabilities,
    CriterionInput,
    RunShoppingPipelineInput,
    StartShoppingWorkflowInput,
)
from personal_shopping_agent.mcp.server import (
    create_default_server,
    create_jd_pipeline_server_for_database,
    create_live_jd_server,
    create_live_jd_server_for_database,
    create_mcp_server,
    create_server_for_database,
)

__all__ = [
    "AgentCapabilities",
    "CriterionInput",
    "RunShoppingPipelineInput",
    "StartShoppingWorkflowInput",
    "create_default_server",
    "create_jd_pipeline_server_for_database",
    "create_live_jd_server",
    "create_live_jd_server_for_database",
    "create_mcp_server",
    "create_server_for_database",
]
