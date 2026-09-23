"""`/v1/calendar-events` -- list (by required date range), create, get,
cancel (Phase 2.6 brief §15/§8; the list route added in Phase 2.15 for the
frontend's calendar/agenda view, brief §11). No `DELETE`: cancellation is a
state transition, never a hard delete."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from voiceagent.api.errors import conflict, not_found
from voiceagent.calendars.errors import (
    CalendarEventConflictError,
    CalendarEventNotFoundError,
    CalendarNotFoundError,
    InvalidIntervalError,
    NaiveDatetimeError,
)
from voiceagent.calendars.models import CalendarEvent
from voiceagent.calendars.permissions import CALENDAR_EVENTS_RESOURCE
from voiceagent.calendars.service import cancel_event, create_event, get_event, list_events
from voiceagent.contacts.errors import ContactNotFoundError
from voiceagent.tenancy import TenantContext, require_tenant

router = APIRouter(prefix="/calendar-events", tags=["calendar-events"])

_read = require_tenant(CALENDAR_EVENTS_RESOURCE, "read")
_create = require_tenant(CALENDAR_EVENTS_RESOURCE, "create")
_cancel = require_tenant(CALENDAR_EVENTS_RESOURCE, "cancel")


class CalendarEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    calendar_id: uuid.UUID
    contact_id: uuid.UUID | None
    title: str
    start_at: datetime
    end_at: datetime
    status: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, event: CalendarEvent) -> CalendarEventOut:
        return cls.model_validate(event)


class CalendarEventCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendar_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    start_at: AwareDatetime
    end_at: AwareDatetime
    contact_id: uuid.UUID | None = None


@router.get("")
def list_calendar_events_route(
    start_at: AwareDatetime,
    end_at: AwareDatetime,
    calendar_id: uuid.UUID | None = Query(default=None),  # noqa: B008 -- FastAPI's own Query idiom
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(_read),  # noqa: B008
) -> list[CalendarEventOut]:
    """A required, bounded date range (never an unbounded full-calendar
    listing) -- mirrors `GET /v1/calendars/{id}/availability`'s own
    `start_at`/`end_at` query-parameter shape."""
    try:
        events = list_events(
            context,
            start_at=start_at,
            end_at=end_at,
            calendar_id=calendar_id,
            limit=limit,
            offset=offset,
        )
    except CalendarNotFoundError:
        raise not_found("calendar") from None
    except (InvalidIntervalError, NaiveDatetimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from None
    return [CalendarEventOut.from_model(event) for event in events]


@router.post("", status_code=201)
def create_calendar_event_route(
    payload: CalendarEventCreateRequest,
    context: TenantContext = Depends(_create),  # noqa: B008
) -> CalendarEventOut:
    try:
        event = create_event(
            context,
            calendar_id=payload.calendar_id,
            title=payload.title,
            start_at=payload.start_at,
            end_at=payload.end_at,
            contact_id=payload.contact_id,
        )
    except CalendarNotFoundError:
        raise not_found("calendar") from None
    except ContactNotFoundError:
        raise not_found("contact") from None
    except (InvalidIntervalError, NaiveDatetimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from None
    except CalendarEventConflictError:
        raise conflict("Requested interval conflicts with an existing appointment.") from None
    return CalendarEventOut.from_model(event)


@router.get("/{event_id}")
def get_calendar_event_route(
    event_id: uuid.UUID,
    context: TenantContext = Depends(_read),  # noqa: B008
) -> CalendarEventOut:
    try:
        event = get_event(context, event_id)
    except CalendarEventNotFoundError:
        raise not_found("calendar event") from None
    return CalendarEventOut.from_model(event)


@router.post("/{event_id}/cancel")
def cancel_calendar_event_route(
    event_id: uuid.UUID,
    context: TenantContext = Depends(_cancel),  # noqa: B008
) -> CalendarEventOut:
    try:
        event = cancel_event(context, event_id)
    except CalendarEventNotFoundError:
        raise not_found("calendar event") from None
    return CalendarEventOut.from_model(event)
