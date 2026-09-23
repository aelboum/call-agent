"""Application service for `Calendar`/`CalendarEvent` (Phase 2.6 brief §8).

Timezone semantics (brief §6): a `Calendar.timezone` must be a real IANA
identifier, validated here with the standard library's own `zoneinfo`
against the `tzdata` package -- never a hand-rolled allowlist. An
`start_at`/`end_at` that is not timezone-aware is rejected here, at the
service boundary, not merely at the API's Pydantic layer -- a future second
caller of this service (the `calendar.create_appointment` tool handler)
gets the identical guarantee for free, exactly the way
`voiceagent.tools.handlers.TransferInput`'s E.164 validation protects every
caller of that model, not just the API.

Overlap semantics (brief §7): `existing.start_at < requested.end_at AND
existing.end_at > requested.start_at`, over `status = 'scheduled'` rows in
the *requested* calendar only -- adjacent appointments (`10:00-10:30`,
`10:30-11:00`) do not conflict, and a cancelled event never blocks.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from voiceagent.calendars.errors import (
    CalendarEventConflictError,
    CalendarEventNotFoundError,
    CalendarNotFoundError,
    InvalidIntervalError,
    InvalidTimezoneError,
    NaiveDatetimeError,
)
from voiceagent.calendars.models import Calendar, CalendarEvent
from voiceagent.contacts.errors import ContactNotFoundError
from voiceagent.contacts.models import Contact
from voiceagent.db import select
from voiceagent.tenancy import TenantContext, tenant_scope

__all__ = [
    "AvailabilityResult",
    "cancel_event",
    "check_availability",
    "create_calendar",
    "create_event",
    "get_calendar",
    "get_event",
    "list_calendars",
    "list_events",
    "validate_timezone",
]


@dataclass(frozen=True, slots=True)
class AvailabilityResult:
    """A provider-neutral availability answer (brief §7). When unavailable,
    carries only the minimum conflict information needed -- the conflicting
    interval, never the conflicting event's id, title, or contact (brief
    §7: "never expose the complete calendar")."""

    available: bool
    conflict_start_at: datetime | None = None
    conflict_end_at: datetime | None = None


def validate_timezone(timezone: str) -> None:
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidTimezoneError() from exc


def _require_aware(*values: datetime) -> None:
    for value in values:
        if value.tzinfo is None or value.utcoffset() is None:
            raise NaiveDatetimeError()


def _require_valid_interval(start_at: datetime, end_at: datetime) -> None:
    _require_aware(start_at, end_at)
    if not start_at < end_at:
        raise InvalidIntervalError()


def _get_calendar_row(session, tenant_id: uuid.UUID, calendar_id: uuid.UUID) -> Calendar:
    row = session.get(Calendar, calendar_id)
    if row is None or row.tenant_id != tenant_id:
        raise CalendarNotFoundError(calendar_id)
    return row


def _get_event_row(session, tenant_id: uuid.UUID, event_id: uuid.UUID) -> CalendarEvent:
    row = session.get(CalendarEvent, event_id)
    if row is None or row.tenant_id != tenant_id:
        raise CalendarEventNotFoundError(event_id)
    return row


def _require_contact_row(session, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> None:
    row = session.get(Contact, contact_id)
    if row is None or row.tenant_id != tenant_id:
        raise ContactNotFoundError(contact_id)


def create_calendar(
    context: TenantContext, *, name: str, timezone: str, is_active: bool = True
) -> Calendar:
    validate_timezone(timezone)
    with tenant_scope(context) as session:
        calendar = Calendar(
            tenant_id=context.tenant_id, name=name, timezone=timezone, is_active=is_active
        )
        session.add(calendar)
        session.flush()
        session.refresh(calendar)
        session.expunge(calendar)
        return calendar


def get_calendar(context: TenantContext, calendar_id: uuid.UUID) -> Calendar:
    with tenant_scope(context) as session:
        row = _get_calendar_row(session, context.tenant_id, calendar_id)
        session.expunge(row)
        return row


def list_calendars(context: TenantContext) -> Sequence[Calendar]:
    with tenant_scope(context) as session:
        rows = (
            session.execute(select(Calendar).where(Calendar.tenant_id == context.tenant_id))
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def _conflicting_event(
    session, tenant_id: uuid.UUID, calendar_id: uuid.UUID, start_at: datetime, end_at: datetime
) -> CalendarEvent | None:
    return (
        session.execute(
            select(CalendarEvent)
            .where(CalendarEvent.tenant_id == tenant_id)
            .where(CalendarEvent.calendar_id == calendar_id)
            .where(CalendarEvent.status == "scheduled")
            .where(CalendarEvent.start_at < end_at)
            .where(CalendarEvent.end_at > start_at)
        )
        .scalars()
        .first()
    )


def check_availability(
    context: TenantContext, calendar_id: uuid.UUID, start_at: datetime, end_at: datetime
) -> AvailabilityResult:
    _require_valid_interval(start_at, end_at)
    with tenant_scope(context) as session:
        _get_calendar_row(session, context.tenant_id, calendar_id)
        conflict = _conflicting_event(session, context.tenant_id, calendar_id, start_at, end_at)
        if conflict is None:
            return AvailabilityResult(available=True)
        return AvailabilityResult(
            available=False,
            conflict_start_at=conflict.start_at,
            conflict_end_at=conflict.end_at,
        )


def create_event(
    context: TenantContext,
    *,
    calendar_id: uuid.UUID,
    title: str,
    start_at: datetime,
    end_at: datetime,
    contact_id: uuid.UUID | None = None,
) -> CalendarEvent:
    """Phase 2.6 brief §8's exact ordering: calendar ownership, optional
    contact ownership, interval validation, conflict check, then create."""
    _require_valid_interval(start_at, end_at)
    with tenant_scope(context) as session:
        _get_calendar_row(session, context.tenant_id, calendar_id)
        if contact_id is not None:
            _require_contact_row(session, context.tenant_id, contact_id)

        conflict = _conflicting_event(session, context.tenant_id, calendar_id, start_at, end_at)
        if conflict is not None:
            raise CalendarEventConflictError()

        event = CalendarEvent(
            tenant_id=context.tenant_id,
            calendar_id=calendar_id,
            contact_id=contact_id,
            title=title,
            start_at=start_at,
            end_at=end_at,
            status="scheduled",
        )
        session.add(event)
        session.flush()
        session.refresh(event)
        session.expunge(event)
        return event


def list_events(
    context: TenantContext,
    *,
    start_at: datetime,
    end_at: datetime,
    calendar_id: uuid.UUID | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> Sequence[CalendarEvent]:
    """Every event in `context`'s tenant whose interval overlaps
    `[start_at, end_at)` (the same overlap test `_conflicting_event()`
    already uses), optionally narrowed to one `calendar_id` -- never an
    unbounded full-calendar scan (Phase 2.15 brief §11: "calendar/agenda
    view", never a second calendar domain model). `limit`/`offset` mirror
    `voiceagent.calls.service.list_call_sessions()`'s own optional,
    keyword-only shape. Ordered by `start_at` ascending (an agenda view's
    natural order), `id` ascending as a stable tie-break."""
    _require_valid_interval(start_at, end_at)
    with tenant_scope(context) as session:
        if calendar_id is not None:
            _get_calendar_row(session, context.tenant_id, calendar_id)
        query = (
            select(CalendarEvent)
            .where(CalendarEvent.tenant_id == context.tenant_id)
            .where(CalendarEvent.start_at < end_at)
            .where(CalendarEvent.end_at > start_at)
        )
        if calendar_id is not None:
            query = query.where(CalendarEvent.calendar_id == calendar_id)
        query = query.order_by(CalendarEvent.start_at.asc(), CalendarEvent.id.asc())
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        rows = session.execute(query).scalars().all()
        for row in rows:
            session.expunge(row)
        return rows


def get_event(context: TenantContext, event_id: uuid.UUID) -> CalendarEvent:
    with tenant_scope(context) as session:
        row = _get_event_row(session, context.tenant_id, event_id)
        session.expunge(row)
        return row


def cancel_event(context: TenantContext, event_id: uuid.UUID) -> CalendarEvent:
    """`scheduled -> cancelled`. Idempotent: cancelling an already-cancelled
    event is a no-op success (brief §8), never a hard delete (brief §5/§7)."""
    with tenant_scope(context) as session:
        event = _get_event_row(session, context.tenant_id, event_id)
        if event.status != "cancelled":
            event.status = "cancelled"
            session.flush()
            session.refresh(event)
        session.expunge(event)
        return event
