"""`app.call_sessions` (Phase 2.0 report §23.5).

Carries call *metadata* and *ownership* only. No conversation storage, no
media-frame storage, no raw audio -- those are Phase 2.2+ tables this phase
deliberately does not create (Phase 2.1 brief §13). Runtime assignment is
represented by `runtime_instance_id`/`runtime_assigned_at` directly on this
table (ADR-0008: a call has exactly one current runtime, no reassignment
history is kept, so a separate `runtime_assignments` table would model a
history this design does not keep).
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
    UUIDPrimaryKeyMixin,
    mapped_column,
    tenant_table_args,
)

__all__ = ["CallSession"]


class CallSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One telephony call, from creation to teardown (Phase 0 report §6,
    Entity: CallSession). `agent_version_id` is resolved once, at creation,
    and this module provides no update path that changes it afterward
    (ADR-0004; Phase 2.1 brief §15) -- it is a plain, non-optional column set
    at construction time, never revisited."""

    __tablename__ = "call_sessions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="initiated", server_default="initiated"
    )
    from_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    to_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    phone_number_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    agent_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: Optional Phase 2.6 association to a `Contact` -- nullable, set only
    #: through `voiceagent.calls.service.associate_call()`, never on the
    #: audio hot path (brief §4/§17).
    contact_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    fs_channel_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runtime_instance_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    runtime_assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hangup_cause: Mapped[str | None] = mapped_column(String(30), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # No FK -- not a primary key of any table; correlates to a
    # core.audit_log entry's own metadata (Phase 2.0 report S23.5).
    data_authorization_decision_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    __table_args__ = tenant_table_args(
        ForeignKeyConstraint(
            ["phone_number_id", "tenant_id"],
            ["app.phone_numbers.id", "app.phone_numbers.tenant_id"],
            name="fk_call_sessions_phone_number",
        ),
        ForeignKeyConstraint(
            ["agent_id", "tenant_id"],
            ["app.agents.id", "app.agents.tenant_id"],
            name="fk_call_sessions_agent",
        ),
        ForeignKeyConstraint(
            ["agent_version_id", "tenant_id"],
            ["app.agent_versions.id", "app.agent_versions.tenant_id"],
            name="fk_call_sessions_agent_version",
        ),
        ForeignKeyConstraint(
            ["contact_id", "tenant_id"],
            ["app.contacts.id", "app.contacts.tenant_id"],
            name="fk_call_sessions_contact",
        ),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="ck_call_sessions_direction"),
        CheckConstraint(
            "status IN ('initiated', 'ringing', 'answered', 'in_progress', "
            "'completed', 'failed', 'interrupted')",
            name="ck_call_sessions_status",
        ),
        Index("ix_call_sessions_tenant_id", "tenant_id"),
        Index("ix_call_sessions_phone_number_id", "phone_number_id"),
        Index("ix_call_sessions_agent_version_id", "agent_version_id"),
        Index("ix_call_sessions_contact_id", "contact_id"),
        Index("ix_call_sessions_status", "status"),
        Index("ix_call_sessions_runtime_instance_id", "runtime_instance_id"),
        # Phase 2.22: replaced by a partial-unique index (migrations/0012) --
        # a second `CallSession` created with the same `fs_channel_uuid` is
        # a database-level conflict, not merely findable by an index. NULL
        # (every outbound call before origination assigns one) stays
        # unconstrained -- see `voiceagent.calls.routing`'s own module
        # docstring and `voiceagent.calls.service.create_call_session()`
        # for the idempotency this enables.
        Index(
            "uq_call_sessions_fs_channel_uuid",
            "fs_channel_uuid",
            unique=True,
            postgresql_where=fs_channel_uuid.isnot(None),
        ),
        # Phase 2.16 security/production-readiness audit (migration 0011):
        # the composite shape `list_non_terminal_call_sessions()`/
        # `list_call_sessions(status=...)` actually filter by -- both
        # single-column indexes above already existed separately, but
        # neither covers this product's own highest-pressure recurring
        # query pattern (reconciliation + stuck-call detection, once per
        # tenant per scan) as efficiently as one composite index.
        Index("ix_call_sessions_tenant_status", "tenant_id", "status"),
    )
