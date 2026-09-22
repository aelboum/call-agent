"""`app.call_ai_analyses` (Phase 2.12).

**One row per analysis *attempt version*, not one row per call**
(`UniqueConstraint(call_session_id, version)`, not
`UniqueConstraint(call_session_id)`). This is the versioning strategy the
brief requires: a `status='completed'` row is never mutated again (enforced
by `app.forbid_call_ai_analysis_completed_update`, migration `0010`,
mirroring `app.forbid_published_agent_version_update`/
`app.forbid_knowledge_item_content_update`) -- a rebuild always inserts a
new, higher-`version` row rather than overwriting history.
`schema_version`/`prompt_version`/`provider`/`model` are captured on every
row so a result is always attributable to exactly what produced it.

**Claim/lease/backoff mechanics are the identical shape
`voiceagent.followups.models.FollowUpAction` already establishes** (Phase
2.9): `next_attempt_at` serves the same three purposes that module's own
docstring documents for its own `next_attempt_at` -- "not due yet" (seeded
to `requested_at` at creation), "lease not yet expired" (set to
`now + LEASE_SECONDS` at claim, letting a crashed worker's claim be
reclaimed once it elapses), and "backing off" (set to
`now + next_attempt_delay_seconds(attempt_count)` on failure). `execution_id`
is the identical stable per-attempt idempotency identity.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from voiceagent.db import (
    JSON,
    Base,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Mapped,
    String,
    TimestampMixin,
    UniqueConstraint,
    UUIDPrimaryKeyMixin,
    mapped_column,
    tenant_table_args,
)

__all__ = [
    "CALL_AI_ANALYSIS_FAILURE_REASONS",
    "CALL_AI_ANALYSIS_STATUSES",
    "CallAiAnalysis",
]

CALL_AI_ANALYSIS_STATUSES = frozenset({"pending", "processing", "completed", "failed"})

#: A short, closed-vocabulary code -- never an exception message, stack
#: trace, or raw provider response (brief SECURITY/AUDIT: never log
#: transcript content, full prompts, or provider responses).
CALL_AI_ANALYSIS_FAILURE_REASONS = frozenset(
    {
        "privacy_denied",
        "call_not_completed",
        "provider_timeout",
        "provider_error",
        "provider_unavailable",
        "malformed_response",
        "unexpected_error",
    }
)


class CallAiAnalysis(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One versioned AI post-call analysis attempt for one call (module
    docstring). `result` is populated only once `status == 'completed'` --
    always a `voiceagent.call_intelligence.schema.CallAiAnalysisResult
    .model_dump(mode="json")`, never arbitrary provider JSON (brief
    STRUCTURED OUTPUT)."""

    __tablename__ = "call_ai_analyses"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    call_session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )

    #: Attributability metadata (brief VERSIONING) -- treated as metadata
    #: only, never leaked into domain logic beyond display/audit.
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    execution_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: See module docstring -- claim lease, backoff deadline, or `NULL` once
    #: exhausted/completed. Never a caller-supplied value.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: `None` until `status == 'completed'`. `JSON(none_as_null=True)`,
    #: matching `voiceagent.conversations.models.ConversationTurn
    #: .tool_payload`'s own documented reason.
    result: Mapped[dict[str, object] | None] = mapped_column(JSON(none_as_null=True), nullable=True)

    __table_args__ = tenant_table_args(
        ForeignKeyConstraint(
            ["call_session_id", "tenant_id"],
            ["app.call_sessions.id", "app.call_sessions.tenant_id"],
            name="fk_call_ai_analyses_call_session",
        ),
        UniqueConstraint(
            "call_session_id", "version", name="uq_call_ai_analyses_call_session_version"
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_call_ai_analyses_status",
        ),
        CheckConstraint("version >= 1", name="ck_call_ai_analyses_version_positive"),
        CheckConstraint(
            "attempt_count >= 0", name="ck_call_ai_analyses_attempt_count_non_negative"
        ),
        CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN ('privacy_denied', "
            "'call_not_completed', 'provider_timeout', 'provider_error', "
            "'provider_unavailable', 'malformed_response', 'unexpected_error')",
            name="ck_call_ai_analyses_failure_reason",
        ),
        CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL AND result IS NOT NULL)",
            name="ck_call_ai_analyses_completed_iff_result",
        ),
        CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)",
            name="ck_call_ai_analyses_failed_iff_reason",
        ),
        Index("ix_call_ai_analyses_tenant_id", "tenant_id"),
        Index("ix_call_ai_analyses_call_session_id", "call_session_id"),
        Index("ix_call_ai_analyses_status", "status"),
        #: The one index `claim_pending_analysis()`'s query needs (brief
        #: WORKER: bounded, indexed due-lookup) -- partial, mirroring
        #: `ix_follow_up_actions_claim_lookup`.
        Index(
            "ix_call_ai_analyses_claim_lookup",
            "tenant_id",
            "next_attempt_at",
            postgresql_where="next_attempt_at IS NOT NULL",
        ),
    )
