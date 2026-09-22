"""`CallAiAnalysisResult` -- the exact, closed, bounded shape a post-call
AI analysis produces (Phase 2.12 brief STRUCTURED OUTPUT: "Do NOT persist
arbitrary provider JSON as the canonical business representation").

Every field has an explicit size/count bound. A provider response that does
not validate against this model is never persisted -- it is a
`malformed_response` failure (`voiceagent.call_intelligence.analyzer`),
exactly the same "the model must reject malformed or oversized output"
discipline `voiceagent.agents.config.AgentConfig`/`voiceagent.workflows
.config.WorkflowDefinition` already apply to their own JSON boundaries.

**`sentiment` is deliberately a single four-value enum, nothing more**
(brief: "do not let it turn into an unconstrained psychological profiling
subsystem") -- no per-emotion breakdown, no numeric intensity score, no
speaker-level attribution. It is optional (`None` when the analyzer chooses
not to produce one) and never a required field.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "MAX_ACTION_ITEMS",
    "MAX_ACTION_ITEM_LENGTH",
    "MAX_ESCALATION_REASON_LENGTH",
    "MAX_INTENT_LENGTH",
    "MAX_SUMMARY_LENGTH",
    "MAX_TOPICS",
    "MAX_TOPIC_LENGTH",
    "CallAiAnalysisResult",
    "EscalationIndicator",
    "SentimentSignal",
]

MAX_SUMMARY_LENGTH = 1000
MAX_INTENT_LENGTH = 300
MAX_TOPICS = 10
MAX_TOPIC_LENGTH = 100
MAX_ACTION_ITEMS = 10
MAX_ACTION_ITEM_LENGTH = 300
MAX_ESCALATION_REASON_LENGTH = 300


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EscalationIndicator(_Strict):
    """A recommendation, never an executed action (brief BUSINESS ACTIONS:
    the analyzer "must NOT directly execute business actions"). `reason` is
    optional regardless of `required`'s value -- an analyzer may recommend
    escalation with or without a stated reason, and may likewise decline to
    explain a non-escalation; neither is treated as malformed."""

    required: bool
    reason: str | None = Field(default=None, max_length=MAX_ESCALATION_REASON_LENGTH)


class SentimentSignal(_Strict):
    """See module docstring -- a single bounded enum, nothing else."""

    overall: Literal["positive", "neutral", "negative", "mixed"]


class CallAiAnalysisResult(_Strict):
    """The canonical, persisted shape of one AI post-call analysis (brief
    STRUCTURED OUTPUT items 1-6). `confidence` is the one required
    quality-metadata field -- a plain `[0.0, 1.0]` float, never a second,
    provider-specific scoring shape."""

    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_LENGTH)
    customer_intent: str = Field(min_length=1, max_length=MAX_INTENT_LENGTH)
    key_topics: list[str] = Field(default_factory=list, max_length=MAX_TOPICS)
    action_items: list[str] = Field(default_factory=list, max_length=MAX_ACTION_ITEMS)
    escalation: EscalationIndicator
    sentiment: SentimentSignal | None = None
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("key_topics")
    @classmethod
    def _bound_topics(cls, value: list[str]) -> list[str]:
        for topic in value:
            if not topic.strip():
                raise ValueError("key_topics entries must not be empty")
            if len(topic) > MAX_TOPIC_LENGTH:
                raise ValueError(f"key_topics entries must be at most {MAX_TOPIC_LENGTH} chars")
        return value

    @field_validator("action_items")
    @classmethod
    def _bound_action_items(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip():
                raise ValueError("action_items entries must not be empty")
            if len(item) > MAX_ACTION_ITEM_LENGTH:
                raise ValueError(
                    f"action_items entries must be at most {MAX_ACTION_ITEM_LENGTH} chars"
                )
        return value
