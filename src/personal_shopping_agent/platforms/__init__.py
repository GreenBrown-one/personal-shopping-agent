"""Replaceable shopping-platform adapters."""

from personal_shopping_agent.platforms.jd import JDSearchAdapter
from personal_shopping_agent.platforms.jd_detail import JDDetailAdapter

__all__ = ["JDDetailAdapter", "JDSearchAdapter"]
