"""`app.call_analysis` (Phase 2.8 brief §3, §9).

One row per `CallSession` (`UNIQUE(call_session_id)`), holding only
deterministic facts computable from already-persisted data --
`voiceagent.call_analysis.service.build_call_analysis()`'s own module
docstring is the authoritative statement of "who owns what" and the exact
metric rules; this model carries no field this phase cannot compute without
guessing.
"""

from __future__ import annotations

import uuid

from voiceagent.db import (
    Base,
    Boolean,
    CheckConstraint,
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

__all__ = ["ANALYSIS_STATUSES", "CallAnalysis"]

#: A small, explicit lifecycle (brief §3) -- deliberately not a job/workflow
#: state machine. This phase's own `build_call_analysis()` always produces
#: `"ready"` directly (it is synchronous and deterministic, so there is no
#: in-flight state it could legitimately return in); `"pending"` is declared
#: here, and permitted by the `CHECK` below, only as a reserved value for a
#: future phase that separates *requesting* an analysis from *computing* it
#: (see `docs/PHASE-2.8-STATUS.md`, "Deviations").
ANALYSIS_STATUSES = frozenset({"pending", "ready"})


class CallAnalysis(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A derived, rebuildable analysis snapshot for one call (brief §3).
    Never authoritative for anything it summarizes -- `call_session_id`,
    `contact_associated` and `outcome` are read from `CallSession`/
    `CallOutcome` at build time, never written back to them."""

    __tablename__ = "call_analysis"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    call_session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ready", server_default="ready"
    )

    # -- deterministic conversation-turn metrics (brief §3/§10) -------------
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    user_turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assistant_turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # -- deterministic call-session facts ------------------------------------
    #: Copied from `CallSession.duration_ms` when available; `None` when the
    #: call never reached a terminal status with both `started_at`/`ended_at`
    #: set -- never invented (brief §3: "do not invent values").
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Derived from `tool_call` turns whose `tool_payload["name"]` is
    #: `"call.transfer"`/`"call.hold"` -- never from parsing the transcript.
    had_transfer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    had_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    contact_associated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # -- outcome/follow-up references (brief §3; never a duplicated domain) -
    #: A point-in-time copy of `CallOutcome.outcome` at build time, or `None`
    #: if the call has no outcome yet -- `voiceagent.followups` remains
    #: authoritative; this column is refreshed by rebuilding, never mutated
    #: independently.
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    follow_up_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    appointment_follow_up_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Follow-ups not in a terminal status (`voiceagent.followups.lifecycle
    #: .TERMINAL_STATUSES`) -- i.e. still `"pending"`.
    open_follow_up_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = tenant_table_args(
        ForeignKeyConstraint(
            ["call_session_id", "tenant_id"],
            ["app.call_sessions.id", "app.call_sessions.tenant_id"],
            name="fk_call_analysis_call_session",
        ),
        UniqueConstraint("call_session_id", name="uq_call_analysis_call_session"),
        CheckConstraint("status IN ('pending', 'ready')", name="ck_call_analysis_status"),
        CheckConstraint("turn_count >= 0", name="ck_call_analysis_turn_count_non_negative"),
        Index("ix_call_analysis_tenant_id", "tenant_id"),
    )
