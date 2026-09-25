"""Controlled browser infrastructure for platform adapters."""

from personal_shopping_agent.sourcing.browser.access import (
    ControlledPageCollector,
    PageAccessRestrictedError,
    PageAccessSettings,
    PageCollectionFailedError,
    StatusPage,
    StatusPageCollector,
)
from personal_shopping_agent.sourcing.browser.manager import (
    BrowserManager,
    BrowserManagerError,
    BrowserManagerSettings,
    BrowserNotStartedError,
    BrowserSnapshot,
    TransientBrowserManagerError,
)
from personal_shopping_agent.sourcing.browser.policy import (
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
    "ControlledPageCollector",
    "NavigationPolicy",
    "NavigationPolicyError",
    "PageAccessRestrictedError",
    "PageAccessSettings",
    "PageCollectionFailedError",
    "StatusPage",
    "StatusPageCollector",
    "TransientBrowserManagerError",
    "ValidatedURL",
]
