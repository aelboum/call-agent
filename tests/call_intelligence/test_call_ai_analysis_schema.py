"""`voiceagent.call_intelligence.schema.CallAiAnalysisResult` (Phase 2.12) --
pure, DB-free pydantic validation, hermetically testable exactly like
`voiceagent.agents.config.AgentConfig`/`voiceagent.workflows.config
.WorkflowDefinition`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from voiceagent.call_intelligence.schema import (
    MAX_ACTION_ITEM_LENGTH,
    MAX_ACTION_ITEMS,
    MAX_INTENT_LENGTH,
    MAX_SUMMARY_LENGTH,
    MAX_TOPIC_LENGTH,
    MAX_TOPICS,
    CallAiAnalysisResult,
)


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "summary": "The customer asked about billing and the issue was resolved.",
        "customer_intent": "Billing inquiry.",
        "key_topics": ["billing", "refund"],
        "action_items": ["Send confirmation email."],
        "escalation": {"required": False, "reason": None},
        "sentiment": {"overall": "neutral"},
        "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


def test_a_valid_result_round_trips() -> None:
    result = CallAiAnalysisResult.model_validate(_valid_payload())
    assert result.summary.startswith("The customer")
    assert result.escalation.required is False
    assert result.sentiment is not None
    assert result.sentiment.overall == "neutral"


def test_sentiment_is_optional() -> None:
    result = CallAiAnalysisResult.model_validate(_valid_payload(sentiment=None))
    assert result.sentiment is None


@pytest.mark.parametrize("missing", ["summary", "customer_intent", "escalation", "confidence"])
def test_missing_required_field_is_rejected(missing: str) -> None:
    payload = _valid_payload()
    del payload[missing]
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate(payload)


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate(_valid_payload(extra_field="not allowed"))


def test_malformed_escalation_shape_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate(_valid_payload(escalation="yes"))


def test_malformed_sentiment_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate(_valid_payload(sentiment={"overall": "ecstatic"}))


def test_oversized_summary_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate(_valid_payload(summary="x" * (MAX_SUMMARY_LENGTH + 1)))


def test_oversized_customer_intent_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate(
            _valid_payload(customer_intent="x" * (MAX_INTENT_LENGTH + 1))
        )


def test_excessive_key_topics_count_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate(
            _valid_payload(key_topics=[f"topic{i}" for i in range(MAX_TOPICS + 1)])
        )


def test_oversized_key_topic_entry_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate(
            _valid_payload(key_topics=["x" * (MAX_TOPIC_LENGTH + 1)])
        )


def test_empty_key_topic_entry_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate(_valid_payload(key_topics=["   "]))


def test_excessive_action_items_count_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate(
            _valid_payload(action_items=[f"item{i}" for i in range(MAX_ACTION_ITEMS + 1)])
        )


def test_oversized_action_item_entry_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate(
            _valid_payload(action_items=["x" * (MAX_ACTION_ITEM_LENGTH + 1)])
        )


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 2.0, -5.0])
def test_invalid_confidence_values_are_rejected(confidence: float) -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate(_valid_payload(confidence=confidence))


@pytest.mark.parametrize("confidence", [0.0, 1.0, 0.5])
def test_boundary_confidence_values_are_accepted(confidence: float) -> None:
    result = CallAiAnalysisResult.model_validate(_valid_payload(confidence=confidence))
    assert result.confidence == confidence


def test_result_is_frozen() -> None:
    result = CallAiAnalysisResult.model_validate(_valid_payload())
    with pytest.raises(ValidationError):
        result.confidence = 0.1  # type: ignore[misc]


def test_malformed_json_text_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate_json("not json at all {")


def test_json_array_instead_of_object_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CallAiAnalysisResult.model_validate_json("[1, 2, 3]")
