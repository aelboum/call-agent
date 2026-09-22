"""`app.call_workflow_executions` (Phase 2.10).

One row per `CallSession` that ever advanced a workflow
(`UNIQUE(call_session_id)`) -- the durable idempotency guard that prevents
two concurrent `workflow.advance` tool calls for the same call from both
executing the workflow (`voiceagent.workflows.service.begin_execution()`).
It is not a generic job/task table: it carries no queue semantics, no
scheduling, no retry/backoff of its own (a workflow execution is a bounded,
synchronous, single-attempt run inside one already-live call, never a
background job -- `voiceagent.workflows.executor`'s own module docstring).

**Recovery boundary, stated plainly** (mirrors `voiceagent.tools.gateway`'s
own documented boundary): if the runtime process crashes mid-execution, this
row is left `status='running'` forever -- there is no lease/reclaim
mechanism here, deliberately, because a crashed runtime process has also
lost the call itself (ADR-0008 point 10/OQ-4: this product implements no
crash takeover of a live call), so there is nothing left to resume the
workflow *for*. The row remains visible, non-misleading evidence of exactly
how far execution got before the crash -- never silently retried, never
silently abandoned.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from voiceagent.db import (
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
    "WORKFLOW_EXECUTION_FAILURE_REASONS",
    "WORKFLOW_EXECUTION_STATUSES",
    "CallWorkflowExecution",
]

WORKFLOW_EXECUTION_STATUSES = frozenset({"running", "completed", "failed", "cancelled"})

#: A short, closed-vocabulary code -- never an exception message, stack
#: trace, or tool payload (brief SECURITY: "Audit metadata MUST NOT
#: contain... exception stack traces").
WORKFLOW_EXECUTION_FAILURE_REASONS = frozenset(
    {
        "tool_step_failed",
        "max_steps_exceeded",
        "execution_conflict",
        "invalid_workflow_definition",
        "unexpected_error",
    }
)


class CallWorkflowExecution(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One call's workflow execution, start to finish. `workflow_config_hash`
    is a point-in-time copy of the governing `AgentVersion.config_hash` at
    the moment execution began -- proof, without a second join, of exactly
    which immutable workflow snapshot this execution ran (ADR-0004)."""

    __tablename__ = "call_workflow_executions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    call_session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    agent_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    workflow_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running", server_default="running"
    )
    current_step_id: Mapped[str] = mapped_column(String(100), nullable=False)
    steps_executed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: `voiceagent.workflows.models.WORKFLOW_EXECUTION_FAILURE_REASONS` --
    #: set only when `status == 'failed'`.
    failure_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)

    __table_args__ = tenant_table_args(
        ForeignKeyConstraint(
            ["call_session_id", "tenant_id"],
            ["app.call_sessions.id", "app.call_sessions.tenant_id"],
            name="fk_call_workflow_executions_call_session",
        ),
        ForeignKeyConstraint(
            ["agent_version_id", "tenant_id"],
            ["app.agent_versions.id", "app.agent_versions.tenant_id"],
            name="fk_call_workflow_executions_agent_version",
        ),
        UniqueConstraint("call_session_id", name="uq_call_workflow_executions_call_session"),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name="ck_call_workflow_executions_status",
        ),
        CheckConstraint(
            "steps_executed >= 0", name="ck_call_workflow_executions_steps_executed_non_negative"
        ),
        CheckConstraint(
            "workflow_config_hash ~ '^[0-9a-f]{64}$'",
            name="ck_call_workflow_executions_config_hash_format",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN ('tool_step_failed', "
            "'max_steps_exceeded', 'execution_conflict', 'invalid_workflow_definition', "
            "'unexpected_error')",
            name="ck_call_workflow_executions_failure_reason",
        ),
        CheckConstraint(
            "(status IN ('completed', 'failed', 'cancelled')) = (ended_at IS NOT NULL)",
            name="ck_call_workflow_executions_ended_at_iff_terminal",
        ),
        Index("ix_call_workflow_executions_tenant_id", "tenant_id"),
        Index("ix_call_workflow_executions_agent_version_id", "agent_version_id"),
        Index("ix_call_workflow_executions_status", "status"),
    )
