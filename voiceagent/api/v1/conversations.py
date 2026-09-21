"""`/v1/call-sessions/{call_session_id}/conversation` -- the one read
endpoint Phase 2.5's brief section 11 asks for: the durable, ordered
conversation history for a call.

Nested under the same `call-sessions` path prefix as
`voiceagent.api.v1.call_sessions`, as a distinct resource -- deliberately
gated by its own permission (`voiceagent.conversations.permissions.RESOURCE`,
not `voiceagent.calls.permissions.RESOURCE`): a role authorized to see call
*metadata* is not automatically authorized to see conversation *content*
(brief section 13; more sensitive data, a narrower default grant).

**A call session id alone is never sufficient** (brief section 12):
`list_conversation_turns()` raises `CallSessionNotFoundError` for a call
that does not belong to the authenticated tenant, mapped to the identical
404 `voiceagent.api.v1.call_sessions` already returns for a foreign
`CallSession` -- no response here can distinguish "wrong tenant" from "no
such call".
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from voiceagent.api.errors import not_found
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.conversations.models import ConversationTurn
from voiceagent.conversations.permissions import RESOURCE
from voiceagent.conversations.service import list_conversation_turns
from voiceagent.tenancy import TenantContext, require_tenant

router = APIRouter(prefix="/call-sessions", tags=["conversations"])

_read = require_tenant(RESOURCE, "read")


class ConversationTurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sequence: int
    role: str
    content: str | None
    tool_payload: dict[str, object] | None
    created_at: datetime

    @classmethod
    def from_model(cls, turn: ConversationTurn) -> ConversationTurnOut:
        return cls.model_validate(turn)


@router.get("/{call_session_id}/conversation")
def get_conversation_route(
    call_session_id: uuid.UUID,
    context: TenantContext = Depends(_read),  # noqa: B008
) -> list[ConversationTurnOut]:
    try:
        turns = list_conversation_turns(context, call_session_id)
    except CallSessionNotFoundError:
        raise not_found("call session") from None
    return [ConversationTurnOut.from_model(turn) for turn in turns]
