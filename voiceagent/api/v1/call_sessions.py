"""`/v1/call-sessions` -- exactly the two read endpoints from Phase 2.0
report §23.9. No creation or transition route: those belong to the Call
Orchestrator (Phase 2.2+), not to a tenant-facing API in Phase 2.1
(`voiceagent.calls.service.create_call_session()`/`transition_call_session()`
exist and are tested directly, but are not exposed here)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from voiceagent.api.errors import not_found
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.calls.lifecycle import VALID_STATUSES
from voiceagent.calls.models import CallSession
from voiceagent.calls.permissions import RESOURCE
from voiceagent.calls.service import get_call_session, list_call_sessions
from voiceagent.tenancy import TenantContext, require_tenant

router = APIRouter(prefix="/call-sessions", tags=["call-sessions"])

_read = require_tenant(RESOURCE, "read")


class CallSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    direction: str
    status: str
    from_e164: str
    to_e164: str
    phone_number_id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    started_at: datetime | None
    answered_at: datetime | None
    ended_at: datetime | None
    duration_ms: int | None
    hangup_cause: str | None
    end_reason: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, call: CallSession) -> CallSessionOut:
        return cls.model_validate(call)


@router.get("/{call_session_id}")
def get_call_session_route(
    call_session_id: uuid.UUID,
    context: TenantContext = Depends(_read),  # noqa: B008
) -> CallSessionOut:
    try:
        call = get_call_session(context, call_session_id)
    except CallSessionNotFoundError:
        raise not_found("call session") from None
    return CallSessionOut.from_model(call)


@router.get("")
def list_call_sessions_route(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: TenantContext = Depends(_read),  # noqa: B008
) -> list[CallSessionOut]:
    """Newest-first, paginated (`limit`/`offset`), optionally filtered to one
    lifecycle `status`. An unrecognized `status` value matches no row (the
    same `CHECK` constraint that governs `CallSession.status` itself) rather
    than being rejected -- there is no distinct-response reason to prefer a
    422 over an empty page here."""
    if status is not None and status not in VALID_STATUSES:
        return []
    return [
        CallSessionOut.from_model(call)
        for call in list_call_sessions(context, status=status, limit=limit, offset=offset)
    ]
