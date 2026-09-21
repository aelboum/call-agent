"""Application service for `PhoneNumber`.

`register_phone_number()` is the one function in this codebase required to
catch a raw `IntegrityError` and translate it before it can reach an API
response (Phase 2.1 brief §12/§24: "no raw DB exception leakage", "the API
must return one generic conflict response"). It distinguishes *which*
constraint fired (by name, via the driver's own diagnostics) only to decide
*internally* whether the failure is the E.164-uniqueness case -- that
distinction is never exposed outward; both "already yours" and "already
someone else's" produce the identical `PhoneNumberUnavailableError`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from voiceagent.db import IntegrityError, select
from voiceagent.phone_numbers.errors import PhoneNumberNotFoundError, PhoneNumberUnavailableError
from voiceagent.phone_numbers.models import PhoneNumber
from voiceagent.tenancy import TenantContext, tenant_scope

__all__ = [
    "get_phone_number",
    "list_phone_numbers",
    "register_phone_number",
    "update_phone_number",
]

_E164_UNIQUE_CONSTRAINT = "uq_phone_numbers_e164_global"


def _get_row(session, tenant_id: uuid.UUID, phone_number_id: uuid.UUID) -> PhoneNumber:
    row = session.get(PhoneNumber, phone_number_id)
    if row is None or row.tenant_id != tenant_id:
        raise PhoneNumberNotFoundError(phone_number_id)
    return row


def register_phone_number(
    context: TenantContext,
    *,
    e164: str,
    label: str | None = None,
    agent_id: uuid.UUID | None = None,
    version_pin_mode: str = "follow_published",
    pinned_version_id: uuid.UUID | None = None,
) -> PhoneNumber:
    try:
        with tenant_scope(context) as session:
            number = PhoneNumber(
                tenant_id=context.tenant_id,
                e164=e164,
                label=label,
                agent_id=agent_id,
                version_pin_mode=version_pin_mode,
                pinned_version_id=pinned_version_id,
            )
            session.add(number)
            session.flush()
            session.refresh(number)
            session.expunge(number)
            return number
    except IntegrityError as exc:
        constraint = _constraint_name(exc)
        if constraint == _E164_UNIQUE_CONSTRAINT:
            # Deliberately the SAME error whether this tenant already owns
            # the number or a different tenant does -- see errors.py.
            raise PhoneNumberUnavailableError() from None
        # A different constraint (e.g. a bad agent_id) is still a client
        # error, but not the privacy-sensitive one -- re-raise as-is rather
        # than inventing a second generic message this module has no
        # evidence is needed yet.
        raise


def _constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failing constraint's name from the
    underlying psycopg diagnostics, without leaking any of it outward --
    used only to decide internally which domain error to raise."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def get_phone_number(context: TenantContext, phone_number_id: uuid.UUID) -> PhoneNumber:
    with tenant_scope(context) as session:
        row = _get_row(session, context.tenant_id, phone_number_id)
        session.expunge(row)
        return row


def list_phone_numbers(context: TenantContext) -> Sequence[PhoneNumber]:
    with tenant_scope(context) as session:
        rows = (
            session.execute(select(PhoneNumber).where(PhoneNumber.tenant_id == context.tenant_id))
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def update_phone_number(
    context: TenantContext,
    phone_number_id: uuid.UUID,
    *,
    label: str | None = None,
    agent_id: uuid.UUID | None = None,
    inbound_enabled: bool | None = None,
    version_pin_mode: str | None = None,
    pinned_version_id: uuid.UUID | None = None,
) -> PhoneNumber:
    with tenant_scope(context) as session:
        row = _get_row(session, context.tenant_id, phone_number_id)
        if label is not None:
            row.label = label
        if agent_id is not None:
            row.agent_id = agent_id
        if inbound_enabled is not None:
            row.inbound_enabled = inbound_enabled
        if version_pin_mode is not None:
            row.version_pin_mode = version_pin_mode
        if pinned_version_id is not None:
            row.pinned_version_id = pinned_version_id
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row
