"""Runtime-only configuration and composition for optional LLM explanations."""

import os
from collections.abc import Mapping
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from personal_shopping_agent.application.explanation import (
    ReportExplanationProvider,
    ReportExplanationRequestBuilder,
    ShoppingReportExplanationService,
    ShoppingReportPresentationService,
)
from personal_shopping_agent.llm.deepseek_adapter import DeepSeekReportExplanationAdapter
from personal_shopping_agent.llm.openai_adapter import OpenAIReportExplanationAdapter

LLM_PROVIDER_ENV = "PERSONAL_SHOPPING_LLM_PROVIDER"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_MODEL_ENV = "PERSONAL_SHOPPING_OPENAI_MODEL"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_MODEL_ENV = "PERSONAL_SHOPPING_DEEPSEEK_MODEL"
LLM_TIMEOUT_ENV = "PERSONAL_SHOPPING_LLM_TIMEOUT_SECONDS"
LLM_OUTPUT_TOKENS_ENV = "PERSONAL_SHOPPING_LLM_MAX_OUTPUT_TOKENS"
LLM_CANDIDATES_ENV = "PERSONAL_SHOPPING_LLM_MAX_CANDIDATES"


class ExplanationProviderKind(StrEnum):
    """Supported runtime provider choices; disabled is the safe default."""

    DISABLED = "disabled"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"


class LLMConfigurationErrorCode(StrEnum):
    """Sanitized configuration failures that never include environment values."""

    INVALID_PROVIDER = "invalid_provider"
    MISSING_API_KEY = "missing_api_key"
    MISSING_MODEL = "missing_model"
    INVALID_MODEL = "invalid_model"
    INVALID_TIMEOUT = "invalid_timeout"
    INVALID_OUTPUT_TOKEN_LIMIT = "invalid_output_token_limit"
    INVALID_CANDIDATE_LIMIT = "invalid_candidate_limit"


class LLMConfigurationError(ValueError):
    """Safe runtime configuration error containing only a stable code."""

    def __init__(self, code: LLMConfigurationErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class LLMExplanationSettings(BaseModel):
    """Validated in-memory settings whose API key is excluded from dumps and reprs."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    provider: ExplanationProviderKind = ExplanationProviderKind.DISABLED
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    model: str | None = Field(default=None, min_length=1, max_length=160)
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    maximum_output_tokens: int = Field(default=2_000, ge=256, le=4_096)
    maximum_candidates: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def provider_credentials_are_consistent(self) -> Self:
        """Reject enabled providers without credentials and disabled secret retention."""

        if self.provider is ExplanationProviderKind.DISABLED:
            if self.api_key is not None or self.model is not None:
                raise ValueError("disabled LLM settings cannot retain provider credentials")
        elif self.api_key is None or self.model is None:
            raise ValueError("enabled LLM settings require an API key and model")
        return self


def _provider_environment_names(provider: ExplanationProviderKind) -> tuple[str, str]:
    if provider is ExplanationProviderKind.OPENAI:
        return OPENAI_API_KEY_ENV, OPENAI_MODEL_ENV
    assert provider is ExplanationProviderKind.DEEPSEEK
    return DEEPSEEK_API_KEY_ENV, DEEPSEEK_MODEL_ENV


def _bounded_float(
    environment: Mapping[str, str],
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
    error_code: LLMConfigurationErrorCode,
) -> float:
    raw = environment.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise LLMConfigurationError(error_code) from None
    if not minimum < value <= maximum:
        raise LLMConfigurationError(error_code)
    return value


def _bounded_int(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
    error_code: LLMConfigurationErrorCode,
) -> int:
    raw = environment.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise LLMConfigurationError(error_code) from None
    if not minimum <= value <= maximum:
        raise LLMConfigurationError(error_code)
    return value


def load_llm_explanation_settings(
    environment: Mapping[str, str] | None = None,
) -> LLMExplanationSettings:
    """Load only the selected provider's key and return sanitized failures."""

    source = os.environ if environment is None else environment
    provider_value = source.get(LLM_PROVIDER_ENV, ExplanationProviderKind.DISABLED.value)
    try:
        provider = ExplanationProviderKind(provider_value.strip().lower())
    except ValueError:
        raise LLMConfigurationError(LLMConfigurationErrorCode.INVALID_PROVIDER) from None
    if provider is ExplanationProviderKind.DISABLED:
        return LLMExplanationSettings()

    api_key_name, model_name = _provider_environment_names(provider)
    api_key = source.get(api_key_name)
    if api_key is None or not api_key.strip():
        raise LLMConfigurationError(LLMConfigurationErrorCode.MISSING_API_KEY)
    model = source.get(model_name)
    if model is None or not model.strip():
        raise LLMConfigurationError(LLMConfigurationErrorCode.MISSING_MODEL)
    normalized_model = model.strip()
    if len(normalized_model) > 160:
        raise LLMConfigurationError(LLMConfigurationErrorCode.INVALID_MODEL)

    timeout_seconds = _bounded_float(
        source,
        LLM_TIMEOUT_ENV,
        default=30,
        minimum=0,
        maximum=120,
        error_code=LLMConfigurationErrorCode.INVALID_TIMEOUT,
    )
    maximum_output_tokens = _bounded_int(
        source,
        LLM_OUTPUT_TOKENS_ENV,
        default=2_000,
        minimum=256,
        maximum=4_096,
        error_code=LLMConfigurationErrorCode.INVALID_OUTPUT_TOKEN_LIMIT,
    )
    maximum_candidates = _bounded_int(
        source,
        LLM_CANDIDATES_ENV,
        default=3,
        minimum=1,
        maximum=10,
        error_code=LLMConfigurationErrorCode.INVALID_CANDIDATE_LIMIT,
    )
    return LLMExplanationSettings(
        provider=provider,
        api_key=SecretStr(api_key.strip()),
        model=normalized_model,
        timeout_seconds=timeout_seconds,
        maximum_output_tokens=maximum_output_tokens,
        maximum_candidates=maximum_candidates,
    )


def create_report_explanation_provider(
    settings: LLMExplanationSettings,
) -> ReportExplanationProvider | None:
    """Instantiate only the explicitly selected outer-layer provider adapter."""

    if settings.provider is ExplanationProviderKind.DISABLED:
        return None
    assert settings.api_key is not None
    assert settings.model is not None
    if settings.provider is ExplanationProviderKind.OPENAI:
        return OpenAIReportExplanationAdapter(
            api_key=settings.api_key.get_secret_value(),
            model=settings.model,
            timeout_seconds=settings.timeout_seconds,
            maximum_output_tokens=settings.maximum_output_tokens,
        )
    return DeepSeekReportExplanationAdapter(
        api_key=settings.api_key.get_secret_value(),
        model=settings.model,
        timeout_seconds=settings.timeout_seconds,
        maximum_output_tokens=settings.maximum_output_tokens,
    )


def create_report_presentation_service(
    settings: LLMExplanationSettings,
) -> ShoppingReportPresentationService:
    """Compose runtime configuration with provider-neutral explanation services."""

    provider = create_report_explanation_provider(settings)
    explanation_service = ShoppingReportExplanationService(
        provider,
        request_builder=ReportExplanationRequestBuilder(
            maximum_candidates=settings.maximum_candidates
        ),
    )
    return ShoppingReportPresentationService(explanation_service)


def create_report_presentation_service_from_environment(
    environment: Mapping[str, str] | None = None,
) -> ShoppingReportPresentationService:
    """Load runtime settings and compose a report presentation service."""

    return create_report_presentation_service(load_llm_explanation_settings(environment))
