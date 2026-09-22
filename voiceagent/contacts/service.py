"""Application service for `Contact` (Phase 2.6 brief §9).

`normalize_phone_e164()` is deliberately small: it strips the punctuation a
human commonly types (spaces, hyphens, parentheses) and then requires the
result to already match E.164 -- it is not a general phone-parsing library
(no libphonenumber dependency; brief §25 does not ask for one) and never
guesses a country code for a number that omits one.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

from voiceagent.contacts.errors import (
    ContactInvalidPhoneError,
    ContactNotFoundError,
    ContactPhoneConflictError,
)
from voiceagent.contacts.models import Contact
from voiceagent.db import IntegrityError, select
from voiceagent.tenancy import TenantContext, tenant_scope

__all__ = [
    "create_contact",
    "get_contact",
    "list_contacts",
    "lookup_contact_by_phone",
    "normalize_phone_e164",
]

#: E.164: a leading `+`, a non-zero first digit, up to 15 digits total --
#: matches `voiceagent.tools.handlers._E164_PATTERN` and
#: `ck_contacts_phone_e164_format`.
_E164_PATTERN = re.compile(r"^\+[1-9]\d{1,14}$")
_STRIPPABLE = re.compile(r"[\s\-().]")

_PHONE_UNIQUE_CONSTRAINT = "uq_contacts_tenant_phone"


def normalize_phone_e164(raw: str) -> str:
    """Strip common human punctuation, then require the result to already be
    valid E.164. Raises `ContactInvalidPhoneError` rather than guessing a
    missing `+`/country code -- an ambiguous number must be rejected, not
    silently reinterpreted."""
    candidate = _STRIPPABLE.sub("", raw)
    if not _E164_PATTERN.match(candidate):
        raise ContactInvalidPhoneError()
    return candidate


def _get_row(session, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> Contact:
    row = session.get(Contact, contact_id)
    if row is None or row.tenant_id != tenant_id:
        raise ContactNotFoundError(contact_id)
    return row


def create_contact(
    context: TenantContext,
    *,
    name: str,
    phone_e164: str,
    email: str | None = None,
) -> Contact:
    normalized = normalize_phone_e164(phone_e164)
    try:
        with tenant_scope(context) as session:
            contact = Contact(
                tenant_id=context.tenant_id, name=name, phone_e164=normalized, email=email
            )
            session.add(contact)
            session.flush()
            session.refresh(contact)
            session.expunge(contact)
            return contact
    except IntegrityError as exc:
        constraint = _constraint_name(exc)
        if constraint == _PHONE_UNIQUE_CONSTRAINT:
            raise ContactPhoneConflictError() from None
        raise


def _constraint_name(exc: IntegrityError) -> str | None:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def get_contact(context: TenantContext, contact_id: uuid.UUID) -> Contact:
    with tenant_scope(context) as session:
        row = _get_row(session, context.tenant_id, contact_id)
        session.expunge(row)
        return row


def list_contacts(context: TenantContext) -> Sequence[Contact]:
    with tenant_scope(context) as session:
        rows = (
            session.execute(select(Contact).where(Contact.tenant_id == context.tenant_id))
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def lookup_contact_by_phone(context: TenantContext, phone_e164: str) -> Contact | None:
    """`None` for "not found" -- never raises, so both the API's
    `GET /v1/contacts/by-phone/{phone}` route and the `contact.lookup_by_phone`
    tool can turn this into their own "not found" shape without a `try`/
    `except` for the ordinary case (brief §10: "explicit not-found result").
    Never returns a contact belonging to a different tenant: RLS scopes the
    query to `context.tenant_id` regardless of what `phone_e164` matches
    elsewhere."""
    try:
        normalized = normalize_phone_e164(phone_e164)
    except ContactInvalidPhoneError:
        return None
    with tenant_scope(context) as session:
        row = (
            session.execute(
                select(Contact)
                .where(Contact.tenant_id == context.tenant_id)
                .where(Contact.phone_e164 == normalized)
            )
            .scalars()
            .first()
        )
        if row is not None:
            session.expunge(row)
        return row
