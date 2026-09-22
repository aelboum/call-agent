"""`/v1/calendars` -- create, list, get, and availability (Phase 2.6 brief
§15). Availability is a `GET` with query parameters, matching this
codebase's existing style for a read-only, filterable query
(`GET /v1/call-sessions?status=...`) rather than inventing a `POST` for what
is not a write."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from voiceagent.api.errors import not_found
from voiceagent.calendars.errors import (
    CalendarNotFoundError,
    InvalidIntervalError,
    InvalidTimezoneError,
    NaiveDatetimeError,
)
from voiceagent.calendars.models import Calendar
from voiceagent.calendars.permissions import CALENDARS_RESOURCE
from voiceagent.calendars.service import (
    check_availability,
    create_calendar,
    get_calendar,
    list_calendars,
)
from voiceagent.tenancy import TenantContext, require_tenant

router = APIRouter(prefix="/calendars", tags=["calendars"])

_read = require_tenant(CALENDARS_RESOURCE, "read")
_create = require_tenant(CALENDARS_RESOURCE, "create")


class CalendarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    timezone: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, calendar: Calendar) -> CalendarOut:
        return cls.model_validate(calendar)


class CalendarCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(min_length=1, max_length=64)
    is_active: bool = True


class AvailabilityOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    conflict_start_at: datetime | None = None
    conflict_end_at: datetime | None = None


@router.post("", status_code=201)
def create_calendar_route(
    payload: CalendarCreateRequest,
    context: TenantContext = Depends(_create),  # noqa: B008
) -> CalendarOut:
    try:
        calendar = create_calendar(
            context, name=payload.name, timezone=payload.timezone, is_active=payload.is_active
        )
    except InvalidTimezoneError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid IANA timezone identifier.",
        ) from None
    return CalendarOut.from_model(calendar)


@router.get("")
def list_calendars_route(
    context: TenantContext = Depends(_read),  # noqa: B008
) -> list[CalendarOut]:
    return [CalendarOut.from_model(calendar) for calendar in list_calendars(context)]


@router.get("/{calendar_id}")
def get_calendar_route(
    calendar_id: uuid.UUID,
    context: TenantContext = Depends(_read),  # noqa: B008
) -> CalendarOut:
    try:
        calendar = get_calendar(context, calendar_id)
    except CalendarNotFoundError:
        raise not_found("calendar") from None
    return CalendarOut.from_model(calendar)


@router.get("/{calendar_id}/availability")
def check_availability_route(
    calendar_id: uuid.UUID,
    start_at: AwareDatetime,
    end_at: AwareDatetime,
    context: TenantContext = Depends(_read),  # noqa: B008
) -> AvailabilityOut:
    try:
        result = check_availability(context, calendar_id, start_at, end_at)
    except CalendarNotFoundError:
        raise not_found("calendar") from None
    except (InvalidIntervalError, NaiveDatetimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from None
    return AvailabilityOut(
        available=result.available,
        conflict_start_at=result.conflict_start_at,
        conflict_end_at=result.conflict_end_at,
    )
