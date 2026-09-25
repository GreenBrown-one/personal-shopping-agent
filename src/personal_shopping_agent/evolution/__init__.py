"""Layer 5 - controlled self-improvement.

Builds local, sanitized improvement case previews for the user to review. It never uploads data,
edits code, or runs inside the MCP runtime; code changes stay with maintenance AI on reviewed
branches. No other layer may import this package.
"""

from personal_shopping_agent.evolution.improvement import (
    CASE_FORMAT,
    PRIVACY_NOTICE,
    ImprovementCase,
    ImprovementCaseBuilder,
    ImprovementOutcome,
    InvalidImprovementCodeError,
    RequestShape,
    is_stable_code,
)

__all__ = [
    "CASE_FORMAT",
    "PRIVACY_NOTICE",
    "ImprovementCase",
    "ImprovementCaseBuilder",
    "ImprovementOutcome",
    "InvalidImprovementCodeError",
    "RequestShape",
    "is_stable_code",
]
