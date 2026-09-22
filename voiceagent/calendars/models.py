"""`app.calendars` / `app.calendar_events` (Phase 2.6 brief §5).

A tenant may own multiple `Calendar` rows -- no "one global calendar per
tenant" assumption anywhere in this schema (brief §5). `CalendarEvent` is
the one appointment/event shape this phase implements: no recurrence, no
attendees, no arbitrary JSON blob (brief §5's explicit non-goals). A
cancelled event is a state transition (`status`), never a row deletion
(brief §5/§7: "cancelled events remain durable").
"""

from __future__ import annotations

import uuid
from datetime import datetime

from voiceagent.db import (
    Base,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Mapped,
    String,
    TimestampMixin,
    UniqueConstraint,
    UUIDPrimaryKeyMixin,
    mapped_column,
    tenant_table_args,
)

__all__ = ["Calendar", "CalendarEvent"]


class Calendar(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A tenant-owned calendar (Phase 2.6 brief §5). `timezone` is an IANA
    identifier, validated at the application boundary
    (`voiceagent.calendars.service.validate_timezone()`) -- this model
    carries no provider-specific field of any kind (brief §18)."""

    __tablename__ = "calendars"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    __table_args__ = tenant_table_args(
        UniqueConstraint("id", "tenant_id", name="uq_calendars_id_tenant"),
        Index("ix_calendars_tenant_id", "tenant_id"),
    )


class CalendarEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One appointment (Phase 2.6 brief §5). `contact_id` is optional --
    calendar events work without a contact, exactly like a `CallSession`
    works without one (§4). `start_at < end_at` is enforced both here and by
    `voiceagent.calendars.service.create_event()`'s own validation, matching
    this product's existing "database CHECK plus application validation,
    never one alone" discipline (e.g. `agent_versions.config_hash`)."""

    __tablename__ = "calendar_events"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    calendar_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduled", server_default="scheduled"
    )

    __table_args__ = tenant_table_args(
        ForeignKeyConstraint(
            ["calendar_id", "tenant_id"],
            ["app.calendars.id", "app.calendars.tenant_id"],
            name="fk_calendar_events_calendar",
        ),
        ForeignKeyConstraint(
            ["contact_id", "tenant_id"],
            ["app.contacts.id", "app.contacts.tenant_id"],
            name="fk_calendar_events_contact",
        ),
        CheckConstraint("start_at < end_at", name="ck_calendar_events_interval"),
        CheckConstraint("status IN ('scheduled', 'cancelled')", name="ck_calendar_events_status"),
        Index("ix_calendar_events_tenant_id", "tenant_id"),
        Index("ix_calendar_events_calendar_id", "calendar_id"),
        Index("ix_calendar_events_contact_id", "contact_id"),
        Index("ix_calendar_events_calendar_window", "calendar_id", "start_at", "end_at"),
    )
