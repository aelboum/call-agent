"""`/v1/follow-ups` -- list, get, complete, cancel, retry (Phase 2.7 brief
§16; Phase 2.9 brief §12 adds list-by-status and retry). No `DELETE`:
cancellation is a state transition, never a hard delete. Creation is
call-scoped and lives on `voiceagent.api.v1.call_sessions`
(`POST /v1/call-sessions/{id}/follow-ups`) -- a follow-up always belongs to
a call, so it is created through that call's own sub-resource, mirroring
`voiceagent.api.v1.calendar_events`'s split from
`voiceagent.api.v1.calendars`. `GET /v1/call-sessions/{id}/follow-ups`
(call-scoped) is unchanged; `GET /v1/follow-ups` here is tenant-scoped, for
due/status visibility across every call (brief §12) -- it is a read, not a
scheduler API: no caller-supplied cron/interval, no bulk operation, just a
filtered, bounded list.

`POST /v1/follow-ups/{id}/retry` (brief §12/§13) is the one administrative
execution control this phase exposes, behind its own dedicated `retry`
permission -- never `execute`/`claim`, which stay unreachable through any
route (`voiceagent.followups.worker.FollowUpWorker` is the only caller of
those)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from voiceagent.api.errors import conflict, not_found
from voiceagent.followups.errors import (
    FollowUpActionNotFoundError,
    FollowUpNotRetryableError,
    InvalidFollowUpStatusError,
    InvalidFollowUpTransitionError,
)
from voiceagent.followups.models import FollowUpAction
from voiceagent.followups.permissions import FOLLOW_UP_ACTIONS_RESOURCE
from voiceagent.followups.service import (
    cancel_follow_up,
    complete_follow_up,
    get_follow_up,
    list_follow_ups_by_status,
    reprocess_follow_up,
)
from voiceagent.tenancy import TenantContext, require_tenant

router = APIRouter(prefix="/follow-ups", tags=["follow-ups"])

_read = require_tenant(FOLLOW_UP_ACTIONS_RESOURCE, "read")
_complete = require_tenant(FOLLOW_UP_ACTIONS_RESOURCE, "complete")
_cancel = require_tenant(FOLLOW_UP_ACTIONS_RESOURCE, "cancel")
_retry = require_tenant(FOLLOW_UP_ACTIONS_RESOURCE, "retry")


class FollowUpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    call_session_id: uuid.UUID
    contact_id: uuid.UUID | None
    type: str
    status: str
    due_at: datetime | None
    calendar_event_id: uuid.UUID | None
    description: str | None
    #: Phase 2.9 execution visibility (brief §12) -- `execution_id` is
    #: deliberately not exposed: internal idempotency plumbing, not
    #: something an API consumer acts on.
    attempt_count: int
    next_attempt_at: datetime | None
    last_attempted_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: FollowUpAction) -> FollowUpOut:
        return cls.model_validate(row)


@router.get("")
def list_follow_ups_by_status_route(
    context: TenantContext = Depends(_read),  # noqa: B008
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[FollowUpOut]:
    """Tenant-scoped due/status listing (brief §12) -- not the call-scoped
    `GET /v1/call-sessions/{id}/follow-ups`."""
    try:
        rows = list_follow_ups_by_status(context, status=status_filter)
    except InvalidFollowUpStatusError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid follow-up status."
        ) from None
    return [FollowUpOut.from_model(row) for row in rows]


@router.get("/{follow_up_id}")
def get_follow_up_route(
    follow_up_id: uuid.UUID,
    context: TenantContext = Depends(_read),  # noqa: B008
) -> FollowUpOut:
    try:
        row = get_follow_up(context, follow_up_id)
    except FollowUpActionNotFoundError:
        raise not_found("follow-up") from None
    return FollowUpOut.from_model(row)


@router.post("/{follow_up_id}/complete")
def complete_follow_up_route(
    follow_up_id: uuid.UUID,
    context: TenantContext = Depends(_complete),  # noqa: B008
) -> FollowUpOut:
    try:
        row = complete_follow_up(context, follow_up_id)
    except FollowUpActionNotFoundError:
        raise not_found("follow-up") from None
    except InvalidFollowUpTransitionError:
        raise conflict("Follow-up cannot be completed from its current status.") from None
    return FollowUpOut.from_model(row)


@router.post("/{follow_up_id}/cancel")
def cancel_follow_up_route(
    follow_up_id: uuid.UUID,
    context: TenantContext = Depends(_cancel),  # noqa: B008
) -> FollowUpOut:
    try:
        row = cancel_follow_up(context, follow_up_id)
    except FollowUpActionNotFoundError:
        raise not_found("follow-up") from None
    except InvalidFollowUpTransitionError:
        raise conflict("Follow-up cannot be cancelled from its current status.") from None
    return FollowUpOut.from_model(row)


@router.post("/{follow_up_id}/retry")
def retry_follow_up_route(
    follow_up_id: uuid.UUID,
    context: TenantContext = Depends(_retry),  # noqa: B008
) -> FollowUpOut:
    """Administrative reprocessing of a `status='failed'` follow-up (brief
    §12/§13) -- clears its backoff wait so the next worker poll claims it
    immediately; refuses once `voiceagent.followups.retry_policy
    .MAX_ATTEMPTS` is already exhausted."""
    try:
        row = reprocess_follow_up(context, follow_up_id)
    except FollowUpActionNotFoundError:
        raise not_found("follow-up") from None
    except FollowUpNotRetryableError:
        raise conflict("Follow-up is not retryable.") from None
    return FollowUpOut.from_model(row)
