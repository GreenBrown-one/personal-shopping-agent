"""Static host guidance for turning the user's own words into one structured request."""

SHOPPING_REQUEST_GUIDE = """Prepare one shopping request from the user's own words.

First call shopping_agent_status and do not assume unavailable capabilities exist. Identify the
product category, normal budget, optional stretch budget, currency, region, and explicit criteria.
The query field is sent verbatim to the platform search box, so keep it to a few search keywords.
Then call review_shopping_request with the draft. For every blocking issue ask the user one concise
follow-up based on its suggestion; relay warnings when they change what the report can show. Never
fill in a bound, unit, or key the user did not confirm. Preserve uncertainty instead of inventing
specifications or prices. After confirmation, call start_shopping_workflow with only the user's
stated constraints. When shopping_agent_status reports collection_mode "assisted", call
list_pages_to_save and ask the user to open and save each unsaved URL before run_shopping_pipeline;
repeat until the pipeline completes. Never claim that collection, ranking, purchase, or payment
occurred unless the corresponding registered tool actually succeeds.
"""
