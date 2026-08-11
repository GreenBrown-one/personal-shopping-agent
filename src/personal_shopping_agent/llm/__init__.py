"""Replaceable LLM adapters for optional report explanations."""

from personal_shopping_agent.llm.deepseek_adapter import DeepSeekReportExplanationAdapter
from personal_shopping_agent.llm.openai_adapter import OpenAIReportExplanationAdapter

__all__ = ["DeepSeekReportExplanationAdapter", "OpenAIReportExplanationAdapter"]
