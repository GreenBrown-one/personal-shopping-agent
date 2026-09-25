"""OpenAI Responses API adapter for typed shopping-report explanations."""

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


class _ParsedResponse(Protocol):
    output_parsed: object


class _ResponsesAPI(Protocol):
    def parse(
        self,
        *,
        model: str,
        input: list[dict[str, str]],
        text_format: type[ShoppingReportExplanation],
        max_output_tokens: int,
        store: bool,
    ) -> _ParsedResponse: ...


class _OpenAIClient(Protocol):
    @property
    def responses(self) -> _ResponsesAPI: ...


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
        raise ValueError("OpenAI model name must contain 1 to 160 characters")
    if not client_provided and (api_key is None or not api_key.strip()):
        raise ValueError("OpenAI API key is required when no client is provided")
    if not 0 < timeout_seconds <= 120:
        raise ValueError("OpenAI timeout must be greater than 0 and at most 120 seconds")
    if not 256 <= maximum_output_tokens <= 4_096:
        raise ValueError("OpenAI output token limit must be between 256 and 4096")
    return normalized_model


class OpenAIReportExplanationAdapter:
    """Call OpenAI Structured Outputs and return only a parsed Pydantic model."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        timeout_seconds: float = 30,
        maximum_output_tokens: int = 2_000,
        client: _OpenAIClient | None = None,
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
            _OpenAIClient,
            OpenAI(
                api_key=api_key,
                timeout=timeout_seconds,
                max_retries=0,
            ),
        )

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def model_name(self) -> str:
        return self._model_name

    def explain(self, request: ReportExplanationRequest) -> ShoppingReportExplanation:
        """Use the Responses API's Pydantic parser and sanitize provider failures."""

        try:
            response = self._client.responses.parse(
                model=self._model_name,
                input=explanation_messages(request),
                text_format=ShoppingReportExplanation,
                max_output_tokens=self._maximum_output_tokens,
                store=False,
            )
        except OpenAIError:
            raise ReportExplanationProviderError(
                ExplanationFallbackReason.PROVIDER_UNAVAILABLE
            ) from None
        try:
            return ShoppingReportExplanation.model_validate(response.output_parsed)
        except ValidationError:
            raise ReportExplanationProviderError(ExplanationFallbackReason.INVALID_OUTPUT) from None
