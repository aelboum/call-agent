"""`/v1/follow-ups` -- get, complete, cancel (Phase 2.7 brief §16). No
`DELETE`: cancellation is a state transition, never a hard delete. Creation
and listing are call-scoped and live on `voiceagent.api.v1.call_sessions`
(`POST`/`GET /v1/call-sessions/{id}/follow-ups`) -- a follow-up always
belongs to a call, so it is created through that call's own sub-resource,
mirroring `voiceagent.api.v1.calendar_events`'s split from
`voiceagent.api.v1.calendars`."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from voiceagent.api.errors import conflict, not_found
from voiceagent.followups.errors import FollowUpActionNotFoundError, InvalidFollowUpTransitionError
from voiceagent.followups.models import FollowUpAction
from voiceagent.followups.permissions import FOLLOW_UP_ACTIONS_RESOURCE
from voiceagent.followups.service import cancel_follow_up, complete_follow_up, get_follow_up
from voiceagent.tenancy import TenantContext, require_tenant

router = APIRouter(prefix="/follow-ups", tags=["follow-ups"])

_read = require_tenant(FOLLOW_UP_ACTIONS_RESOURCE, "read")
_complete = require_tenant(FOLLOW_UP_ACTIONS_RESOURCE, "complete")
_cancel = require_tenant(FOLLOW_UP_ACTIONS_RESOURCE, "cancel")


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
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: FollowUpAction) -> FollowUpOut:
        return cls.model_validate(row)


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
