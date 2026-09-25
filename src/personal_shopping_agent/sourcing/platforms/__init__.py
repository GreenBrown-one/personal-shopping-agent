"""Replaceable shopping-platform adapters."""

from personal_shopping_agent.sourcing.platforms.jd import JDSearchAdapter
from personal_shopping_agent.sourcing.platforms.jd_detail import JDDetailAdapter

__all__ = ["JDDetailAdapter", "JDSearchAdapter"]
