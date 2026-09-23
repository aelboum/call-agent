"""`/v1/contacts` -- create, get, lookup-by-phone (Phase 2.6 brief §15/§9),
plus list (Phase 2.15 brief §10 -- the frontend's contacts list/search
foundation; `list_contacts()` already existed as a service function, used
only by `voiceagent.contacts` callers outside the API, and is wired to a
route here for the first time). No delete, no fuzzy search."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from voiceagent.api.errors import conflict, not_found
from voiceagent.contacts.errors import (
    ContactInvalidPhoneError,
    ContactNotFoundError,
    ContactPhoneConflictError,
)
from voiceagent.contacts.models import Contact
from voiceagent.contacts.permissions import RESOURCE
from voiceagent.contacts.service import (
    create_contact,
    get_contact,
    list_contacts,
    lookup_contact_by_phone,
)
from voiceagent.tenancy import TenantContext, require_tenant

router = APIRouter(prefix="/contacts", tags=["contacts"])

_read = require_tenant(RESOURCE, "read")
_create = require_tenant(RESOURCE, "create")


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    phone_e164: str
    email: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, contact: Contact) -> ContactOut:
        return cls.model_validate(contact)


class ContactCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    phone_e164: str = Field(min_length=1, max_length=32)
    email: str | None = None


@router.post("", status_code=201)
def create_contact_route(
    payload: ContactCreateRequest,
    context: TenantContext = Depends(_create),  # noqa: B008
) -> ContactOut:
    try:
        contact = create_contact(
            context, name=payload.name, phone_e164=payload.phone_e164, email=payload.email
        )
    except ContactInvalidPhoneError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid E.164 phone number."
        ) from None
    except ContactPhoneConflictError:
        raise conflict("Contact with this phone number already exists.") from None
    return ContactOut.from_model(contact)


@router.get("")
def list_contacts_route(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(_read),  # noqa: B008
) -> list[ContactOut]:
    return [
        ContactOut.from_model(contact)
        for contact in list_contacts(context, limit=limit, offset=offset)
    ]


@router.get("/by-phone/{phone_e164}")
def lookup_contact_by_phone_route(
    phone_e164: str,
    context: TenantContext = Depends(_read),  # noqa: B008
) -> ContactOut:
    contact = lookup_contact_by_phone(context, phone_e164)
    if contact is None:
        raise not_found("contact")
    return ContactOut.from_model(contact)


@router.get("/{contact_id}")
def get_contact_route(
    contact_id: uuid.UUID,
    context: TenantContext = Depends(_read),  # noqa: B008
) -> ContactOut:
    try:
        contact = get_contact(context, contact_id)
    except ContactNotFoundError:
        raise not_found("contact") from None
    return ContactOut.from_model(contact)
