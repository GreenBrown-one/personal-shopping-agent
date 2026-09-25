"""Tests for runtime-only LLM provider selection and safe composition."""

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

import personal_shopping_agent.presentation.llm.config as config_module
from personal_shopping_agent.presentation import (
    ReportExplanationRequest,
    ShoppingReportExplanation,
    ShoppingReportPresentationService,
)
from personal_shopping_agent.presentation.llm import (
    ExplanationProviderKind,
    LLMConfigurationError,
    LLMConfigurationErrorCode,
    LLMExplanationSettings,
    create_report_explanation_provider,
    create_report_presentation_service,
    create_report_presentation_service_from_environment,
    load_llm_explanation_settings,
)
from personal_shopping_agent.presentation.llm.config import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_MODEL_ENV,
    LLM_CANDIDATES_ENV,
    LLM_OUTPUT_TOKENS_ENV,
    LLM_PROVIDER_ENV,
    LLM_TIMEOUT_ENV,
    OPENAI_API_KEY_ENV,
    OPENAI_MODEL_ENV,
)


def provider_environment(provider: str = "openai") -> dict[str, str]:
    if provider == "openai":
        return {
            LLM_PROVIDER_ENV: provider,
            OPENAI_API_KEY_ENV: "runtime-secret",
            OPENAI_MODEL_ENV: "gpt-example",
        }
    return {
        LLM_PROVIDER_ENV: provider,
        DEEPSEEK_API_KEY_ENV: "runtime-secret",
        DEEPSEEK_MODEL_ENV: "deepseek-example",
    }


def test_disabled_is_default_and_does_not_read_unselected_secrets() -> None:
    settings = load_llm_explanation_settings(
        {
            OPENAI_API_KEY_ENV: "must-not-be-loaded",
            OPENAI_MODEL_ENV: "unused-model",
        }
    )

    assert settings.provider is ExplanationProviderKind.DISABLED
    assert settings.api_key is None and settings.model is None
    assert "api_key" not in settings.model_dump()
    assert "must-not-be-loaded" not in repr(settings)


def test_environment_none_uses_process_environment_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        LLM_PROVIDER_ENV,
        OPENAI_API_KEY_ENV,
        OPENAI_MODEL_ENV,
        DEEPSEEK_API_KEY_ENV,
        DEEPSEEK_MODEL_ENV,
    ):
        monkeypatch.delenv(name, raising=False)

    assert load_llm_explanation_settings().provider is ExplanationProviderKind.DISABLED


def test_openai_settings_load_defaults_and_hide_secret() -> None:
    settings = load_llm_explanation_settings(provider_environment())

    assert settings.provider is ExplanationProviderKind.OPENAI
    assert settings.model == "gpt-example"
    assert settings.timeout_seconds == 30
    assert settings.maximum_output_tokens == 2_000
    assert settings.maximum_candidates == 3
    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == "runtime-secret"
    assert "runtime-secret" not in repr(settings)
    assert "api_key" not in settings.model_dump()


def test_deepseek_settings_load_bounded_runtime_values() -> None:
    environment = provider_environment("deepseek")
    environment.update(
        {
            LLM_TIMEOUT_ENV: "45.5",
            LLM_OUTPUT_TOKENS_ENV: "1024",
            LLM_CANDIDATES_ENV: "5",
        }
    )

    settings = load_llm_explanation_settings(environment)

    assert settings.provider is ExplanationProviderKind.DEEPSEEK
    assert settings.model == "deepseek-example"
    assert settings.timeout_seconds == 45.5
    assert settings.maximum_output_tokens == 1_024
    assert settings.maximum_candidates == 5


@pytest.mark.parametrize(
    ("environment", "code"),
    [
        ({LLM_PROVIDER_ENV: "unknown"}, LLMConfigurationErrorCode.INVALID_PROVIDER),
        ({LLM_PROVIDER_ENV: "openai"}, LLMConfigurationErrorCode.MISSING_API_KEY),
        (
            {LLM_PROVIDER_ENV: "openai", OPENAI_API_KEY_ENV: " "},
            LLMConfigurationErrorCode.MISSING_API_KEY,
        ),
        (
            {LLM_PROVIDER_ENV: "openai", OPENAI_API_KEY_ENV: "secret"},
            LLMConfigurationErrorCode.MISSING_MODEL,
        ),
        (
            {
                LLM_PROVIDER_ENV: "openai",
                OPENAI_API_KEY_ENV: "secret",
                OPENAI_MODEL_ENV: " ",
            },
            LLMConfigurationErrorCode.MISSING_MODEL,
        ),
        (
            {
                **provider_environment(),
                OPENAI_MODEL_ENV: "x" * 161,
            },
            LLMConfigurationErrorCode.INVALID_MODEL,
        ),
        (
            {**provider_environment(), LLM_TIMEOUT_ENV: "invalid"},
            LLMConfigurationErrorCode.INVALID_TIMEOUT,
        ),
        (
            {**provider_environment(), LLM_TIMEOUT_ENV: "0"},
            LLMConfigurationErrorCode.INVALID_TIMEOUT,
        ),
        (
            {**provider_environment(), LLM_TIMEOUT_ENV: "121"},
            LLMConfigurationErrorCode.INVALID_TIMEOUT,
        ),
        (
            {**provider_environment(), LLM_OUTPUT_TOKENS_ENV: "invalid"},
            LLMConfigurationErrorCode.INVALID_OUTPUT_TOKEN_LIMIT,
        ),
        (
            {**provider_environment(), LLM_OUTPUT_TOKENS_ENV: "255"},
            LLMConfigurationErrorCode.INVALID_OUTPUT_TOKEN_LIMIT,
        ),
        (
            {**provider_environment(), LLM_OUTPUT_TOKENS_ENV: "4097"},
            LLMConfigurationErrorCode.INVALID_OUTPUT_TOKEN_LIMIT,
        ),
        (
            {**provider_environment(), LLM_CANDIDATES_ENV: "invalid"},
            LLMConfigurationErrorCode.INVALID_CANDIDATE_LIMIT,
        ),
        (
            {**provider_environment(), LLM_CANDIDATES_ENV: "0"},
            LLMConfigurationErrorCode.INVALID_CANDIDATE_LIMIT,
        ),
        (
            {**provider_environment(), LLM_CANDIDATES_ENV: "11"},
            LLMConfigurationErrorCode.INVALID_CANDIDATE_LIMIT,
        ),
    ],
)
def test_invalid_environment_values_return_only_sanitized_codes(
    environment: dict[str, str],
    code: LLMConfigurationErrorCode,
) -> None:
    with pytest.raises(LLMConfigurationError) as error:
        load_llm_explanation_settings(environment)

    assert error.value.code is code
    assert str(error.value) == code.value
    assert "secret" not in str(error.value)


def test_settings_reject_inconsistent_direct_construction() -> None:
    with pytest.raises(ValidationError, match="cannot retain"):
        LLMExplanationSettings(
            provider=ExplanationProviderKind.DISABLED,
            api_key=SecretStr("secret"),
            model="unused",
        )
    with pytest.raises(ValidationError, match="require an API key"):
        LLMExplanationSettings(provider=ExplanationProviderKind.OPENAI)


@dataclass
class FakeAdapter:
    provider_name: str
    model_name: str
    configuration: dict[str, object]

    def explain(self, request: ReportExplanationRequest) -> ShoppingReportExplanation:
        del request
        raise AssertionError("factory tests must not call an external provider")


def test_provider_factory_selects_only_requested_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_openai(**kwargs: object) -> FakeAdapter:
        calls.append(("openai", kwargs))
        return FakeAdapter("openai", str(kwargs["model"]), kwargs)

    def fake_deepseek(**kwargs: object) -> FakeAdapter:
        calls.append(("deepseek", kwargs))
        return FakeAdapter("deepseek", str(kwargs["model"]), kwargs)

    monkeypatch.setattr(config_module, "OpenAIReportExplanationAdapter", fake_openai)
    monkeypatch.setattr(config_module, "DeepSeekReportExplanationAdapter", fake_deepseek)

    assert create_report_explanation_provider(LLMExplanationSettings()) is None
    openai_settings = load_llm_explanation_settings(provider_environment())
    deepseek_settings = load_llm_explanation_settings(provider_environment("deepseek"))
    openai_provider = create_report_explanation_provider(openai_settings)
    deepseek_provider = create_report_explanation_provider(deepseek_settings)

    assert isinstance(openai_provider, FakeAdapter)
    assert isinstance(deepseek_provider, FakeAdapter)
    assert [name for name, _ in calls] == ["openai", "deepseek"]
    assert calls[0][1] == {
        "api_key": "runtime-secret",
        "model": "gpt-example",
        "timeout_seconds": 30,
        "maximum_output_tokens": 2_000,
    }
    assert calls[1][1]["model"] == "deepseek-example"


def test_runtime_composition_builds_presentation_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_openai(**kwargs: Any) -> FakeAdapter:
        return FakeAdapter("openai", str(kwargs["model"]), kwargs)

    monkeypatch.setattr(config_module, "OpenAIReportExplanationAdapter", fake_openai)
    settings = load_llm_explanation_settings({**provider_environment(), LLM_CANDIDATES_ENV: "2"})

    direct = create_report_presentation_service(settings)
    from_environment = create_report_presentation_service_from_environment(
        {**provider_environment(), LLM_CANDIDATES_ENV: "2"}
    )

    assert isinstance(direct, ShoppingReportPresentationService)
    assert isinstance(from_environment, ShoppingReportPresentationService)
