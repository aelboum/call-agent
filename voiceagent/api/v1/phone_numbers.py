"""`/v1/phone-numbers` -- exactly the endpoints from Phase 2.0 report §23.9:
create (claim), list, update. No delete route is specified.

`create_phone_number_route()` is the one handler in this codebase whose
*whole purpose* is the generic-conflict guarantee (Phase 2.1 brief §12):
`PhoneNumberUnavailableError` carries no detail, and this handler adds none
either -- the same `conflict()` response for a number this tenant already
claimed and a number a different tenant claimed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from voiceagent.api.errors import conflict, not_found
from voiceagent.phone_numbers.errors import PhoneNumberNotFoundError, PhoneNumberUnavailableError
from voiceagent.phone_numbers.models import PhoneNumber
from voiceagent.phone_numbers.permissions import RESOURCE
from voiceagent.phone_numbers.service import (
    list_phone_numbers,
    register_phone_number,
    update_phone_number,
)
from voiceagent.tenancy import TenantContext, require_tenant

router = APIRouter(prefix="/phone-numbers", tags=["phone-numbers"])

_read = require_tenant(RESOURCE, "read")
_write = require_tenant(RESOURCE, "write")


class PhoneNumberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    e164: str
    label: str | None
    agent_id: uuid.UUID | None
    version_pin_mode: str
    pinned_version_id: uuid.UUID | None
    inbound_enabled: bool
    outbound_caller_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, number: PhoneNumber) -> PhoneNumberOut:
        return cls.model_validate(number)


class PhoneNumberCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    e164: str = Field(min_length=1, max_length=20)
    label: str | None = None
    agent_id: uuid.UUID | None = None


class PhoneNumberUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    agent_id: uuid.UUID | None = None
    inbound_enabled: bool | None = None
    version_pin_mode: str | None = None
    pinned_version_id: uuid.UUID | None = None


@router.post("", status_code=201)
def create_phone_number_route(
    payload: PhoneNumberCreateRequest,
    context: TenantContext = Depends(_write),  # noqa: B008
) -> PhoneNumberOut:
    try:
        number = register_phone_number(
            context, e164=payload.e164, label=payload.label, agent_id=payload.agent_id
        )
    except PhoneNumberUnavailableError:
        # Identical response regardless of who owns the conflicting number.
        raise conflict("Phone number unavailable.") from None
    return PhoneNumberOut.from_model(number)


@router.get("")
def list_phone_numbers_route(
    context: TenantContext = Depends(_read),  # noqa: B008
) -> list[PhoneNumberOut]:
    return [PhoneNumberOut.from_model(number) for number in list_phone_numbers(context)]


@router.patch("/{phone_number_id}")
def update_phone_number_route(
    phone_number_id: uuid.UUID,
    payload: PhoneNumberUpdateRequest,
    context: TenantContext = Depends(_write),  # noqa: B008 -- FastAPI's own dependency idiom
) -> PhoneNumberOut:
    try:
        number = update_phone_number(
            context,
            phone_number_id,
            label=payload.label,
            agent_id=payload.agent_id,
            inbound_enabled=payload.inbound_enabled,
            version_pin_mode=payload.version_pin_mode,
            pinned_version_id=payload.pinned_version_id,
        )
    except PhoneNumberNotFoundError:
        raise not_found("phone number") from None
    return PhoneNumberOut.from_model(number)
