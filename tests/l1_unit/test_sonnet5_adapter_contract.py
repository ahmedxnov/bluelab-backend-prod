"""Sonnet 5 request-shape and pricing contracts for Anthropic adapters."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest
from tools.collect_grading_study import _evaluate_validated

from bluelab.adapters.document_extraction import AzureDocumentExtractionProvider
from bluelab.adapters.evaluator_llm import (
    EVALUATOR_MODEL,
    EVALUATOR_SENTINEL_MODEL,
    AnthropicEvaluatorProvider,
    EvaluationCall,
    FixtureEvaluatorProvider,
    GradingBasis,
)
from bluelab.adapters.generation_llm import AnthropicGenerationProvider
from bluelab.platform.config import Settings

pytestmark = [pytest.mark.l1_unit]

_ANTHROPIC_UNSUPPORTED_SCHEMA_KEYS = {
    "maximum",
    "maxItems",
    "maxLength",
    "minimum",
    "multipleOf",
}


def _schema_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(
            *(_schema_keys(item) for item in value.values()),
        )
    if isinstance(value, list):
        return set().union(*(_schema_keys(item) for item in value))
    return set()


class _CapturingMessages:
    def __init__(self, response: object | list[object]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> object:
        self.requests.append(request)
        return self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "DATABASE_URL": (
            "postgresql+asyncpg://test_user:test_password@db.example.invalid/test_db"
        ),
        "VALKEY_URL": "redis://127.0.0.1:6379/0",
        "AGENT_HMAC_SECRET": "test-secret-not-real",
        "VENDOR_FIXTURE_MODE": False,
        "EVALUATOR_API_KEY": "test-evaluator-key",
        "GENERATION_API_KEY": "test-generation-key",
        "DOCUMENT_INTELLIGENCE_ENDPOINT": "https://documents.invalid",
        "DOCUMENT_INTELLIGENCE_KEY": "test-document-key",
        "DEPENDENCY_MAX_ATTEMPTS": 1,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _basis() -> GradingBasis:
    return GradingBasis.model_validate(
        {
            "attempt_id": "01900000-0000-7000-8000-000000000403",
            "content_hash": "a" * 64,
            "call_type": "discovery",
            "scenario": {"context": "A discovery call."},
            "answer_key": {"facts": ["Terms depend on underwriting."]},
            "rubric": [
                {
                    "dimension_id": "01900000-0000-7000-8000-000000000401",
                    "name": "Discovery",
                    "weight": 100,
                    "rationale": "Surface the buyer need.",
                }
            ],
            "transcript": [
                {
                    "seq": 1,
                    "speaker": "participant",
                    "at_ms": 100,
                    "text": "محتاج أعرف احتياج الفريق.",
                }
            ],
        }
    )


def _evaluator_response(model: str) -> object:
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        content=[
            SimpleNamespace(
                type="tool_use",
                name="submit_grade",
                input={
                    "dimensions": {
                        "dimension_1": {
                            "score_tenths": 70,
                            "english_note": "The participant surfaced the buyer need.",
                            "evidence_seq": [1],
                        }
                    },
                    "english_takeaway": (
                        "Continue grounding discovery in the buyer's words."
                    ),
                    "moments": {
                        "dimension_1": {
                            "severity": "green",
                            "transcript_seq": 1,
                            "english_try_instead": "Keep doing this.",
                            "english_why_it_matters": "This supports buyer trust.",
                        }
                    },
                },
            )
        ],
    )


def _evaluator_response_with_oversized_coaching(model: str) -> object:
    response = _evaluator_response(model)
    response.content[0].input["moments"]["dimension_1"]["severity"] = "amber"
    response.content[0].input["moments"]["dimension_1"][
        "english_why_it_matters"
    ] = "x" * 1_001
    return response


@pytest.mark.verifies("NFR-004", "NFR-006")
async def test_sonnet5_evaluator_disables_thinking_and_uses_strict_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _CapturingMessages(_evaluator_response(EVALUATOR_MODEL))
    monkeypatch.setattr(
        anthropic,
        "AsyncAnthropic",
        lambda **_kwargs: SimpleNamespace(messages=messages),
    )

    call = await AnthropicEvaluatorProvider(_settings()).evaluate(_basis())

    request = messages.requests[0]
    assert request["model"] == "claude-sonnet-5"
    assert request["thinking"] == {"type": "disabled"}
    assert "Arabic-script characters may appear only in quote fields" in request["system"]
    assert not {"temperature", "top_p", "top_k"}.intersection(request)
    assert request["tools"][0]["strict"] is True
    assert not _ANTHROPIC_UNSUPPORTED_SCHEMA_KEYS.intersection(
        _schema_keys(request["tools"][0]["input_schema"])
    )
    schema = request["tools"][0]["input_schema"]
    dimensions_schema = schema["properties"]["dimensions"]
    assert dimensions_schema["type"] == "object"
    assert dimensions_schema["required"] == ["dimension_1"]
    assert dimensions_schema["properties"]["dimension_1"] == {
        "$ref": "#/$defs/WireDimensionJudgment"
    }
    judgment_schema = schema["$defs"]["WireDimensionJudgment"]
    assert "dimension_id" not in judgment_schema["properties"]
    assert "score" not in judgment_schema["properties"]
    assert "note" not in judgment_schema["properties"]
    assert "english_note" in judgment_schema["properties"]
    assert judgment_schema["properties"]["score_tenths"]["type"] == "integer"
    assert judgment_schema["properties"]["evidence_seq"]["items"]["enum"] == [1]
    moments_schema = schema["properties"]["moments"]
    assert moments_schema["type"] == "object"
    assert moments_schema["required"] == ["dimension_1"]
    assert moments_schema["properties"]["dimension_1"] == {
        "$ref": "#/$defs/WireEvaluationMoment"
    }
    moment_schema = schema["$defs"]["WireEvaluationMoment"]
    assert "dimension_id" not in moment_schema["properties"]
    assert "quote" not in moment_schema["properties"]
    assert "try_instead" not in moment_schema["properties"]
    assert "why_it_matters" not in moment_schema["properties"]
    assert set(moment_schema["required"]) == {
        "severity",
        "transcript_seq",
        "english_try_instead",
        "english_why_it_matters",
    }
    assert call.cost_usd == Decimal("0.0007")


@pytest.mark.verifies("NFR-004", "NFR-006")
async def test_evaluator_retries_locally_invalid_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _CapturingMessages(
        [
            _evaluator_response_with_oversized_coaching(EVALUATOR_MODEL),
            _evaluator_response(EVALUATOR_MODEL),
        ]
    )
    monkeypatch.setattr(
        anthropic,
        "AsyncAnthropic",
        lambda **_kwargs: SimpleNamespace(messages=messages),
    )

    call = await AnthropicEvaluatorProvider(
        _settings(
            DEPENDENCY_MAX_ATTEMPTS=2,
            DEPENDENCY_BACKOFF_BASE_SECONDS=0,
        )
    ).evaluate(_basis())

    assert len(messages.requests) == 2
    assert call.cost_usd == Decimal("0.0014")


@pytest.mark.verifies("NFR-004")
async def test_study_collection_mirrors_semantic_job_retries() -> None:
    valid = await FixtureEvaluatorProvider().evaluate(_basis())
    invalid = EvaluationCall(
        output=valid.output.model_copy(update={"takeaway": "تعليق عربي"}),
        model_version=EVALUATOR_MODEL,
        input_tokens=10,
        output_tokens=10,
        cost_usd=Decimal("0.00012"),
    )

    class _Provider:
        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(self, _basis: GradingBasis) -> EvaluationCall:
            self.calls += 1
            return invalid if self.calls == 1 else valid

    provider = _Provider()
    call, evaluation = await _evaluate_validated(provider, _basis())

    assert provider.calls == 2
    assert call is valid
    assert evaluation.takeaway == valid.output.takeaway


@pytest.mark.verifies("NFR-004")
async def test_haiku_sentinel_retains_temperature_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _CapturingMessages(_evaluator_response(EVALUATOR_SENTINEL_MODEL))
    monkeypatch.setattr(
        anthropic,
        "AsyncAnthropic",
        lambda **_kwargs: SimpleNamespace(messages=messages),
    )

    await AnthropicEvaluatorProvider(
        _settings(), model=EVALUATOR_SENTINEL_MODEL
    ).evaluate(_basis())

    request = messages.requests[0]
    assert request["model"] == "claude-haiku-4-5-20251001"
    assert request["temperature"] == 0
    assert "thinking" not in request


@pytest.mark.verifies("FR-DRL-007")
async def test_generation_uses_sonnet5_json_schema_without_sampling_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _CapturingMessages(
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="text",
                    text='{"relevant":true,"reason":"Selling conduct."}',
                )
            ]
        )
    )
    monkeypatch.setattr(
        anthropic,
        "AsyncAnthropic",
        lambda **_kwargs: SimpleNamespace(messages=messages),
    )

    result = await AnthropicGenerationProvider(_settings()).check_relevance(
        "Renewal budget discussion"
    )

    request = messages.requests[0]
    assert result.relevant is True
    assert request["model"] == "claude-sonnet-5"
    assert not {"temperature", "top_p", "top_k"}.intersection(request)
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert not _ANTHROPIC_UNSUPPORTED_SCHEMA_KEYS.intersection(
        _schema_keys(request["output_config"]["format"]["schema"])
    )


@pytest.mark.verifies("FR-KNW-003", "FR-KNW-008")
async def test_fact_structuring_uses_sonnet5_json_schema_without_sampling_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _CapturingMessages(
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="text",
                    text='[{"label":"Rate","value":"10","note":null}]',
                )
            ]
        )
    )
    monkeypatch.setattr(
        anthropic,
        "AsyncAnthropic",
        lambda **_kwargs: SimpleNamespace(messages=messages),
    )

    class _Poller:
        async def result(self) -> object:
            return SimpleNamespace(content="Rate: 10")

    class _DocumentClient:
        async def begin_analyze_document(self, *_args: Any, **_kwargs: Any) -> _Poller:
            return _Poller()

    monkeypatch.setattr(
        "azure.ai.documentintelligence.aio.DocumentIntelligenceClient",
        lambda *_args, **_kwargs: _DocumentClient(),
    )

    facts = await AzureDocumentExtractionProvider(_settings()).extract(
        b"%PDF-synthetic", content_type="application/pdf"
    )

    request = messages.requests[0]
    assert facts[0].value == "10"
    assert request["model"] == "claude-sonnet-5"
    assert not {"temperature", "top_p", "top_k"}.intersection(request)
    assert request["output_config"]["format"] == {
        "type": "json_schema",
        "schema": request["output_config"]["format"]["schema"],
    }
    assert not _ANTHROPIC_UNSUPPORTED_SCHEMA_KEYS.intersection(
        _schema_keys(request["output_config"]["format"]["schema"])
    )
