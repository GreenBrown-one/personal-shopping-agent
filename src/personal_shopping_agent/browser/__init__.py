"""Controlled browser infrastructure for platform adapters."""

from personal_shopping_agent.browser.manager import (
    BrowserManager,
    BrowserManagerError,
    BrowserManagerSettings,
    BrowserNotStartedError,
    BrowserSnapshot,
)
from personal_shopping_agent.browser.policy import (
    NavigationPolicy,
    NavigationPolicyError,
    ValidatedURL,
)

__all__ = [
    "BrowserManager",
    "BrowserManagerError",
    "BrowserManagerSettings",
    "BrowserNotStartedError",
    "BrowserSnapshot",
    "NavigationPolicy",
    "NavigationPolicyError",
    "ValidatedURL",
]
