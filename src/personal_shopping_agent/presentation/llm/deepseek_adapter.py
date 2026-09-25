"""DeepSeek text-only JSON Output adapter for shopping-report explanations."""

from collections.abc import Sequence
from typing import Protocol, cast

from openai import OpenAI, OpenAIError
from pydantic import ValidationError

from personal_shopping_agent.presentation.explanation import (
    ExplanationFallbackReason,
    ReportExplanationProviderError,
    ReportExplanationRequest,
    ShoppingReportExplanation,
)
from personal_shopping_agent.presentation.llm._prompt import explanation_messages

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class _ChatMessage(Protocol):
    @property
    def content(self) -> str | None: ...


class _ChatChoice(Protocol):
    @property
    def message(self) -> _ChatMessage: ...


class _ChatCompletion(Protocol):
    @property
    def choices(self) -> Sequence[_ChatChoice]: ...


class _ChatCompletionsAPI(Protocol):
    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, str],
        max_tokens: int,
        temperature: float,
    ) -> _ChatCompletion: ...


class _ChatAPI(Protocol):
    @property
    def completions(self) -> _ChatCompletionsAPI: ...


class _DeepSeekClient(Protocol):
    @property
    def chat(self) -> _ChatAPI: ...


def _validate_configuration(
    *,
    api_key: str | None,
    model: str,
    client_provided: bool,
    timeout_seconds: float,
    maximum_output_tokens: int,
) -> str:
    normalized_model = model.strip()
    if not normalized_model or len(normalized_model) > 160:
        raise ValueError("DeepSeek model name must contain 1 to 160 characters")
    if not client_provided and (api_key is None or not api_key.strip()):
        raise ValueError("DeepSeek API key is required when no client is provided")
    if not 0 < timeout_seconds <= 120:
        raise ValueError("DeepSeek timeout must be greater than 0 and at most 120 seconds")
    if not 256 <= maximum_output_tokens <= 4_096:
        raise ValueError("DeepSeek output token limit must be between 256 and 4096")
    return normalized_model


class DeepSeekReportExplanationAdapter:
    """Call DeepSeek's OpenAI-compatible Chat Completions JSON mode."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        timeout_seconds: float = 30,
        maximum_output_tokens: int = 2_000,
        client: _DeepSeekClient | None = None,
    ) -> None:
        self._model_name = _validate_configuration(
            api_key=api_key,
            model=model,
            client_provided=client is not None,
            timeout_seconds=timeout_seconds,
            maximum_output_tokens=maximum_output_tokens,
        )
        self._maximum_output_tokens = maximum_output_tokens
        self._client = client or cast(
            _DeepSeekClient,
            OpenAI(
                api_key=api_key,
                base_url=DEEPSEEK_BASE_URL,
                timeout=timeout_seconds,
                max_retries=0,
            ),
        )

    @property
    def provider_name(self) -> str:
        return "deepseek"

    @property
    def model_name(self) -> str:
        return self._model_name

    def explain(self, request: ReportExplanationRequest) -> ShoppingReportExplanation:
        """Parse DeepSeek's valid JSON and then enforce the local Pydantic schema."""

        try:
            response = self._client.chat.completions.create(
                model=self._model_name,
                messages=explanation_messages(request),
                response_format={"type": "json_object"},
                max_tokens=self._maximum_output_tokens,
                temperature=0,
            )
        except OpenAIError:
            raise ReportExplanationProviderError(
                ExplanationFallbackReason.PROVIDER_UNAVAILABLE
            ) from None
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise ReportExplanationProviderError(ExplanationFallbackReason.INVALID_OUTPUT)
        try:
            return ShoppingReportExplanation.model_validate_json(content)
        except ValidationError:
            raise ReportExplanationProviderError(ExplanationFallbackReason.INVALID_OUTPUT) from None
