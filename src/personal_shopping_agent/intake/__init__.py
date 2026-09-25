"""Layer 1 - clarify the user's shopping need.

Deterministic review of a structured request against the same criterion rules scoring uses, plus
static host guidance. Nothing here stores data, calls a model, or accesses the network.
"""

from personal_shopping_agent.intake.guide import SHOPPING_REQUEST_GUIDE
from personal_shopping_agent.intake.review import (
    RequirementClarificationRequiredError,
    RequirementIssue,
    RequirementIssueCode,
    RequirementIssueSeverity,
    RequirementReview,
    RequirementReviewer,
    SupportedMeasurement,
)

__all__ = [
    "SHOPPING_REQUEST_GUIDE",
    "RequirementClarificationRequiredError",
    "RequirementIssue",
    "RequirementIssueCode",
    "RequirementIssueSeverity",
    "RequirementReview",
    "RequirementReviewer",
    "SupportedMeasurement",
]
