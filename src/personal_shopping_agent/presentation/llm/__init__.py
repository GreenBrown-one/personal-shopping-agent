"""Replaceable LLM adapters for optional report explanations."""

from personal_shopping_agent.presentation.llm.config import (
    ExplanationProviderKind,
    LLMConfigurationError,
    LLMConfigurationErrorCode,
    LLMExplanationSettings,
    create_report_explanation_provider,
    create_report_presentation_service,
    create_report_presentation_service_from_environment,
    load_llm_explanation_settings,
)
from personal_shopping_agent.presentation.llm.deepseek_adapter import (
    DeepSeekReportExplanationAdapter,
)
from personal_shopping_agent.presentation.llm.openai_adapter import OpenAIReportExplanationAdapter

__all__ = [
    "DeepSeekReportExplanationAdapter",
    "ExplanationProviderKind",
    "LLMConfigurationError",
    "LLMConfigurationErrorCode",
    "LLMExplanationSettings",
    "OpenAIReportExplanationAdapter",
    "create_report_explanation_provider",
    "create_report_presentation_service",
    "create_report_presentation_service_from_environment",
    "load_llm_explanation_settings",
]
