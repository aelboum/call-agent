"""`app.call_outcomes` / `app.follow_up_actions` (Phase 2.7 brief §5, §7).

A call's business result (`CallOutcome`) is a distinct concept from its
technical lifecycle (`CallSession.status`) -- see `OUTCOME_VALUES` below for
how the two vocabularies are kept from colliding. `FollowUpAction` exists
only to represent an action resulting from a call; it is not a generic task
table (brief §7's own explicit non-goals: no category, priority, assignee,
project, label, workflow id, automation id, recurrence, dependency, or
subtask field exists here or ever will).
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
    Mapped,
    String,
    Text,
    TimestampMixin,
    UniqueConstraint,
    UUIDPrimaryKeyMixin,
    mapped_column,
    tenant_table_args,
)

__all__ = [
    "FOLLOW_UP_STATUSES",
    "FOLLOW_UP_TYPES",
    "OUTCOME_VALUES",
    "CallOutcome",
    "FollowUpAction",
]

#: The business-result vocabulary (brief §5). Deliberately disjoint from
#: `voiceagent.calls.lifecycle.VALID_STATUSES` -- "resolved" stands in for
#: the brief's own example value "completed", which collides with
#: `CallSession.status`'s "completed"; the brief's own §5 explicitly asks
#: for names that do not conflict (see `docs/PHASE-2.7-STATUS.md`
#: "Deviations").
OUTCOME_VALUES = frozenset(
    {
        "resolved",
        "appointment_scheduled",
        "follow_up_required",
        "no_answer",
        "wrong_number",
        "not_interested",
    }
)

FOLLOW_UP_TYPES = frozenset({"appointment", "contact", "manual_follow_up"})
FOLLOW_UP_STATUSES = frozenset({"pending", "completed", "cancelled"})


class CallOutcome(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The business result of one call (brief §5). At most one row per
    `call_session_id` (`uq_call_outcomes_call_session`) -- a changed
    business result updates this row; this phase keeps no outcome history
    (brief §6)."""

    __tablename__ = "call_outcomes"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    call_session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = tenant_table_args(
        ForeignKeyConstraint(
            ["call_session_id", "tenant_id"],
            ["app.call_sessions.id", "app.call_sessions.tenant_id"],
            name="fk_call_outcomes_call_session",
        ),
        ForeignKeyConstraint(
            ["contact_id", "tenant_id"],
            ["app.contacts.id", "app.contacts.tenant_id"],
            name="fk_call_outcomes_contact",
        ),
        UniqueConstraint("call_session_id", name="uq_call_outcomes_call_session"),
        CheckConstraint(
            "outcome IN ('resolved', 'appointment_scheduled', 'follow_up_required', "
            "'no_answer', 'wrong_number', 'not_interested')",
            name="ck_call_outcomes_outcome",
        ),
        Index("ix_call_outcomes_tenant_id", "tenant_id"),
        Index("ix_call_outcomes_contact_id", "contact_id"),
    )


class FollowUpAction(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One explicit follow-up action resulting from a call (brief §7).
    `calendar_event_id` is set if and only if `type == 'appointment'`
    (`ck_follow_up_actions_appointment_requires_calendar_event`, brief §8's
    deterministic relationship rule)."""

    __tablename__ = "follow_up_actions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    call_session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    calendar_event_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = tenant_table_args(
        ForeignKeyConstraint(
            ["call_session_id", "tenant_id"],
            ["app.call_sessions.id", "app.call_sessions.tenant_id"],
            name="fk_follow_up_actions_call_session",
        ),
        ForeignKeyConstraint(
            ["contact_id", "tenant_id"],
            ["app.contacts.id", "app.contacts.tenant_id"],
            name="fk_follow_up_actions_contact",
        ),
        ForeignKeyConstraint(
            ["calendar_event_id", "tenant_id"],
            ["app.calendar_events.id", "app.calendar_events.tenant_id"],
            name="fk_follow_up_actions_calendar_event",
        ),
        CheckConstraint(
            "type IN ('appointment', 'contact', 'manual_follow_up')",
            name="ck_follow_up_actions_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'cancelled')",
            name="ck_follow_up_actions_status",
        ),
        CheckConstraint(
            "(type = 'appointment') = (calendar_event_id IS NOT NULL)",
            name="ck_follow_up_actions_appointment_requires_calendar_event",
        ),
        Index("ix_follow_up_actions_tenant_id", "tenant_id"),
        Index("ix_follow_up_actions_call_session_id", "call_session_id"),
        Index("ix_follow_up_actions_contact_id", "contact_id"),
        Index("ix_follow_up_actions_calendar_event_id", "calendar_event_id"),
        Index("ix_follow_up_actions_status", "status"),
    )
