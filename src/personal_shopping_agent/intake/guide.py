"""Static host guidance for turning the user's own words into one structured request."""

SHOPPING_REQUEST_GUIDE = """Prepare one shopping request from the user's own words.

First call shopping_agent_status and do not assume unavailable capabilities exist. Identify the
product category, normal budget, optional stretch budget, currency, region, and explicit criteria.
Then call review_shopping_request with the draft. For every blocking issue ask the user one concise
follow-up based on its suggestion; relay warnings when they change what the report can show. Never
fill in a bound, unit, or key the user did not confirm. Preserve uncertainty instead of inventing
specifications or prices. After confirmation, call start_shopping_workflow with only the user's
stated constraints. Never claim that collection, ranking, purchase, or payment occurred unless the
corresponding registered tool actually succeeds.
"""
