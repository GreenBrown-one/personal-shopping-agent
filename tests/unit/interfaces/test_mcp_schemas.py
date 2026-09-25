"""Host-facing MCP input schemas explain how each field is used."""

from personal_shopping_agent.intake import SHOPPING_REQUEST_GUIDE
from personal_shopping_agent.interfaces.mcp.schemas import StartShoppingWorkflowInput


def test_query_is_documented_as_verbatim_platform_search_keywords() -> None:
    schema = StartShoppingWorkflowInput.model_json_schema()
    description = schema["properties"]["query"]["description"]

    assert "verbatim" in description
    assert "search" in description
    assert "sent verbatim to the platform search box" in SHOPPING_REQUEST_GUIDE
