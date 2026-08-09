"""Unit tests for OpenAI and DeepSeek report-explanation adapters."""

# ruff: noqa: RUF001 -- Chinese fixture copy intentionally uses full-width punctuation.

import json
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from openai import APIConnectionError, OpenAIError

import personal_shopping_agent.llm.deepseek_adapter as deepseek_module
import personal_shopping_agent.llm.openai_adapter as openai_module
from personal_shopping_agent.application import (
    CandidateExplanation,
    ExplanationFact,
    ExplanationFallbackReason,
    ExplanationStatement,
    ReportExplanationProviderError,
    ReportExplanationRequest,
    ShoppingReportExplanation,
)
from personal_shopping_agent.llm import (
    DeepSeekReportExplanationAdapter,
    OpenAIReportExplanationAdapter,
)


def build_request() -> ReportExplanationRequest:
    product_id = UUID("00000000-0000-0000-0000-000000000001")
    return ReportExplanationRequest(
        report_id=UUID("10000000-0000-0000-0000-000000000001"),
        report_content_sha256="a" * 64,
        candidate_product_ids=(product_id,),
        recommended_product_id=product_id,
        facts=(
            ExplanationFact(
                id="report.recommendation",
                label="推荐",
                value=str(product_id),
            ),
            ExplanationFact(
                id="report.disclaimer",
                label="限制",
                value="购买前复核，系统不下单。",
            ),
            ExplanationFact(
                id=f"candidate.{product_id}.name",
                label="名称",
                value="Example phone",
                candidate_product_id=product_id,
            ),
        ),
    )


def build_output(request: ReportExplanationRequest) -> ShoppingReportExplanation:
    product_id = request.candidate_product_ids[0]
    return ShoppingReportExplanation(
        report_id=request.report_id,
        report_content_sha256=request.report_content_sha256,
        overview=ExplanationStatement(
            text="这是报告的语言解释。",
            fact_ids=("report.recommendation",),
        ),
        candidates=(
            CandidateExplanation(
                product_id=product_id,
                summary=ExplanationStatement(
                    text="候选名称来自报告。",
                    fact_ids=(f"candidate.{product_id}.name",),
                ),
            ),
        ),
        cautions=(
            ExplanationStatement(
                text="购买前请复核。",
                fact_ids=("report.disclaimer",),
            ),
        ),
    )


def connection_error() -> OpenAIError:
    return APIConnectionError(request=httpx.Request("POST", "https://provider.example.com"))


@dataclass
class FakeParsedResponse:
    output_parsed: object


class FakeResponsesAPI:
    def __init__(self, output: object, *, error: OpenAIError | None = None) -> None:
        self.output = output
        self.error = error
        self.call: dict[str, object] | None = None

    def parse(self, **kwargs: Any) -> FakeParsedResponse:
        self.call = kwargs
        if self.error is not None:
            raise self.error
        return FakeParsedResponse(self.output)


@dataclass
class FakeOpenAIClient:
    responses: FakeResponsesAPI


@dataclass
class FakeMessage:
    content: str | None


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeChatCompletion:
    choices: list[FakeChoice]


class FakeChatCompletionsAPI:
    def __init__(
        self,
        response: FakeChatCompletion,
        *,
        error: OpenAIError | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.call: dict[str, object] | None = None

    def create(self, **kwargs: Any) -> FakeChatCompletion:
        self.call = kwargs
        if self.error is not None:
            raise self.error
        return self.response


@dataclass
class FakeChatAPI:
    completions: FakeChatCompletionsAPI


@dataclass
class FakeDeepSeekClient:
    chat: FakeChatAPI


def deepseek_client(
    content: str | None,
    *,
    choices: bool = True,
    error: OpenAIError | None = None,
) -> tuple[FakeDeepSeekClient, FakeChatCompletionsAPI]:
    response = FakeChatCompletion(choices=[FakeChoice(FakeMessage(content))] if choices else [])
    completions = FakeChatCompletionsAPI(response, error=error)
    return FakeDeepSeekClient(FakeChatAPI(completions)), completions


def test_openai_adapter_uses_responses_structured_output_without_storage() -> None:
    request = build_request()
    output = build_output(request)
    responses = FakeResponsesAPI(output)
    adapter = OpenAIReportExplanationAdapter(
        api_key=None,
        model=" gpt-example ",
        client=FakeOpenAIClient(responses),
    )

    result = adapter.explain(request)

    assert result == output
    assert adapter.provider_name == "openai"
    assert adapter.model_name == "gpt-example"
    assert responses.call is not None
    assert responses.call["text_format"] is ShoppingReportExplanation
    assert responses.call["max_output_tokens"] == 2_000
    assert responses.call["store"] is False
    messages = cast(list[dict[str, str]], responses.call["input"])
    assert "不可信数据" in messages[0]["content"]
    assert "JSON" in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert payload["input"]["report_id"] == str(request.report_id)
    assert payload["example_shape"]["candidates"][0]["product_id"] == str(
        request.candidate_product_ids[0]
    )


def test_openai_adapter_maps_provider_and_invalid_output_failures() -> None:
    request = build_request()
    unavailable = OpenAIReportExplanationAdapter(
        api_key=None,
        model="gpt-example",
        client=FakeOpenAIClient(FakeResponsesAPI(None, error=connection_error())),
    )
    with pytest.raises(ReportExplanationProviderError) as unavailable_error:
        unavailable.explain(request)
    assert unavailable_error.value.reason is ExplanationFallbackReason.PROVIDER_UNAVAILABLE

    invalid = OpenAIReportExplanationAdapter(
        api_key=None,
        model="gpt-example",
        client=FakeOpenAIClient(FakeResponsesAPI(None)),
    )
    with pytest.raises(ReportExplanationProviderError) as invalid_error:
        invalid.explain(request)
    assert invalid_error.value.reason is ExplanationFallbackReason.INVALID_OUTPUT


def test_openai_adapter_constructs_official_client_without_retaining_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = build_request()
    output = build_output(request)
    fake_client = FakeOpenAIClient(FakeResponsesAPI(output))
    captured: dict[str, object] = {}

    def fake_openai(**kwargs: object) -> FakeOpenAIClient:
        captured.update(kwargs)
        return fake_client

    monkeypatch.setattr(openai_module, "OpenAI", fake_openai)
    adapter = OpenAIReportExplanationAdapter(api_key="top-secret", model="gpt-example")

    assert adapter.explain(request) == output
    assert captured == {"api_key": "top-secret", "timeout": 30, "max_retries": 0}
    assert "top-secret" not in repr(adapter)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"model": ""}, "model name"),
        ({"model": "x" * 161}, "model name"),
        ({"api_key": None}, "API key"),
        ({"api_key": " "}, "API key"),
        ({"timeout_seconds": 0}, "timeout"),
        ({"timeout_seconds": 121}, "timeout"),
        ({"maximum_output_tokens": 255}, "token limit"),
        ({"maximum_output_tokens": 4_097}, "token limit"),
    ],
)
def test_openai_adapter_rejects_invalid_configuration(
    overrides: dict[str, object],
    message: str,
) -> None:
    parameters: dict[str, object] = {"api_key": "secret", "model": "gpt-example"}
    parameters.update(overrides)
    with pytest.raises(ValueError, match=message):
        OpenAIReportExplanationAdapter(**cast(Any, parameters))


def test_deepseek_adapter_uses_text_only_json_output_then_local_validation() -> None:
    request = build_request()
    output = build_output(request)
    client, completions = deepseek_client(output.model_dump_json())
    adapter = DeepSeekReportExplanationAdapter(
        api_key=None,
        model=" deepseek-example ",
        client=client,
    )

    result = adapter.explain(request)

    assert result == output
    assert adapter.provider_name == "deepseek"
    assert adapter.model_name == "deepseek-example"
    assert completions.call is not None
    assert completions.call["response_format"] == {"type": "json_object"}
    assert completions.call["max_tokens"] == 2_000
    assert completions.call["temperature"] == 0
    messages = cast(list[dict[str, str]], completions.call["messages"])
    assert all(isinstance(message["content"], str) for message in messages)


@pytest.mark.parametrize(
    ("content", "choices"),
    [(None, True), (None, False), ("not JSON", True), ("{}", True)],
)
def test_deepseek_adapter_rejects_empty_or_schema_invalid_json(
    content: str | None,
    choices: bool,
) -> None:
    request = build_request()
    client, _ = deepseek_client(content, choices=choices)
    adapter = DeepSeekReportExplanationAdapter(
        api_key=None,
        model="deepseek-example",
        client=client,
    )

    with pytest.raises(ReportExplanationProviderError) as error:
        adapter.explain(request)

    assert error.value.reason is ExplanationFallbackReason.INVALID_OUTPUT


def test_deepseek_adapter_sanitizes_provider_failure() -> None:
    request = build_request()
    client, _ = deepseek_client(None, error=connection_error())
    adapter = DeepSeekReportExplanationAdapter(
        api_key=None,
        model="deepseek-example",
        client=client,
    )

    with pytest.raises(ReportExplanationProviderError) as error:
        adapter.explain(request)

    assert error.value.reason is ExplanationFallbackReason.PROVIDER_UNAVAILABLE
    assert str(error.value) == "provider_unavailable"


def test_deepseek_adapter_constructs_openai_compatible_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = build_request()
    output = build_output(request)
    fake_client, _ = deepseek_client(output.model_dump_json())
    captured: dict[str, object] = {}

    def fake_openai(**kwargs: object) -> FakeDeepSeekClient:
        captured.update(kwargs)
        return fake_client

    monkeypatch.setattr(deepseek_module, "OpenAI", fake_openai)
    adapter = DeepSeekReportExplanationAdapter(api_key="top-secret", model="deepseek-example")

    assert adapter.explain(request) == output
    assert captured == {
        "api_key": "top-secret",
        "base_url": "https://api.deepseek.com",
        "timeout": 30,
        "max_retries": 0,
    }
    assert "top-secret" not in repr(adapter)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"model": ""}, "model name"),
        ({"model": "x" * 161}, "model name"),
        ({"api_key": None}, "API key"),
        ({"api_key": " "}, "API key"),
        ({"timeout_seconds": 0}, "timeout"),
        ({"timeout_seconds": 121}, "timeout"),
        ({"maximum_output_tokens": 255}, "token limit"),
        ({"maximum_output_tokens": 4_097}, "token limit"),
    ],
)
def test_deepseek_adapter_rejects_invalid_configuration(
    overrides: dict[str, object],
    message: str,
) -> None:
    parameters: dict[str, object] = {"api_key": "secret", "model": "deepseek-example"}
    parameters.update(overrides)
    with pytest.raises(ValueError, match=message):
        DeepSeekReportExplanationAdapter(**cast(Any, parameters))


def test_adapter_unexpected_programming_errors_are_not_hidden() -> None:
    class BrokenResponses:
        def parse(self, **kwargs: Any) -> FakeParsedResponse:
            del kwargs
            raise RuntimeError("programming bug")

    adapter = OpenAIReportExplanationAdapter(
        api_key=None,
        model="gpt-example",
        client=FakeOpenAIClient(cast(FakeResponsesAPI, BrokenResponses())),
    )

    with pytest.raises(RuntimeError, match="programming bug"):
        adapter.explain(build_request())
