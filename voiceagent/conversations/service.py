"""Application service for durable conversation turns (Phase 2.5).

`persist_conversation_turn()` is synchronous, by design: it is the one
function `voiceagent.runtime.conversation_persistence` calls through
`voiceagent.runtime.db.DatabaseBoundary.run()`, exactly the same seam
`voiceagent.calls.service.transition_call_session()` already crosses from
`voiceagent.runtime.call_task`. Nothing in this module is ever called
directly from the audio/media pump.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.calls.models import CallSession
from voiceagent.conversations.errors import InvalidConversationTurnError
from voiceagent.conversations.models import ROLES, ConversationTurn
from voiceagent.db import delete, select
from voiceagent.tenancy import TenantContext, tenant_scope

__all__ = [
    "delete_conversation_turns",
    "list_conversation_turns",
    "persist_conversation_turn",
]


def _lock_call_session(session, tenant_id: uuid.UUID, call_session_id: uuid.UUID) -> CallSession:
    """`SELECT ... FOR UPDATE` on the owning `CallSession` row -- the lock
    that makes per-call sequence assignment atomic against a concurrent
    persistence attempt for the same call (module docstring's "Ordering"),
    the identical primitive `voiceagent.calls.service
    ._get_row_for_update()` already uses for `claim_runtime_ownership()`'s
    own read-then-write invariant."""
    row = session.get(CallSession, call_session_id, with_for_update=True)
    if row is None or row.tenant_id != tenant_id:
        raise CallSessionNotFoundError(call_session_id)
    return row


def persist_conversation_turn(
    context: TenantContext,
    call_session_id: uuid.UUID,
    *,
    event_id: str,
    role: str,
    content: str | None,
    tool_payload: Mapping[str, object] | None,
) -> ConversationTurn:
    """Idempotent: a second call with the same `(call_session_id, event_id,
    role)` returns the already-persisted row unchanged, never a duplicate
    turn and never a second sequence number consumed (brief section 8).
    `role` is part of the idempotency key, not just `event_id`: a tool
    call's own "tool_call" and "tool_result" turns share one `event_id`
    (`ToolCallRequested.call_id`/`ToolResult.call_id`) by design (brief
    section 16 -- reusing the Tool Gateway's own existing idempotency key
    rather than inventing a second one), so `event_id` alone cannot tell
    those two turns apart. The idempotency check and the sequence-number
    computation both happen while holding `_lock_call_session()`'s row
    lock, so there is no window in which two concurrent callers for the
    same call could both observe "not yet persisted" and both insert.

    Raises `CallSessionNotFoundError` if `call_session_id` does not belong
    to `context.tenant_id` -- the same fail-closed behavior every other
    `voiceagent.calls.service`/`voiceagent.agents.service` lookup already
    has, never a silent no-op.
    """
    if role not in ROLES:
        raise InvalidConversationTurnError(role)

    with tenant_scope(context) as session:
        _lock_call_session(session, context.tenant_id, call_session_id)

        existing = (
            session.execute(
                select(ConversationTurn)
                .where(ConversationTurn.tenant_id == context.tenant_id)
                .where(ConversationTurn.call_session_id == call_session_id)
                .where(ConversationTurn.event_id == event_id)
                .where(ConversationTurn.role == role)
            )
            .scalars()
            .one_or_none()
        )
        if existing is not None:
            session.expunge(existing)
            return existing

        # ADR-0007: no sqlalchemy.func. The next sequence number is
        # computed in Python from the (small, per-call) set of existing
        # ones, never via func.max() -- the same discipline
        # voiceagent.agents.service.create_draft_version() already applies
        # to its own version_number.
        existing_sequences = (
            session.execute(
                select(ConversationTurn.sequence).where(
                    ConversationTurn.call_session_id == call_session_id
                )
            )
            .scalars()
            .all()
        )
        next_sequence = (max(existing_sequences) if existing_sequences else -1) + 1

        turn = ConversationTurn(
            tenant_id=context.tenant_id,
            call_session_id=call_session_id,
            event_id=event_id,
            sequence=next_sequence,
            role=role,
            content=content,
            tool_payload=dict(tool_payload) if tool_payload is not None else None,
        )
        session.add(turn)
        session.flush()
        session.refresh(turn)
        session.expunge(turn)
        return turn


def list_conversation_turns(
    context: TenantContext, call_session_id: uuid.UUID
) -> Sequence[ConversationTurn]:
    """Ordered by `sequence`, ascending -- never by timestamp (module
    docstring of `voiceagent.conversations.models`). Raises
    `CallSessionNotFoundError` for a foreign or nonexistent call: a call
    session id alone is never sufficient to read its conversation (Phase 2.5
    brief section 12) -- the caller (`voiceagent.api.v1.conversations`)
    turns that into the identical 404 `voiceagent.api.v1.call_sessions`
    already returns for a foreign `CallSession`, so no response can
    distinguish "wrong tenant" from "no such call"."""
    with tenant_scope(context) as session:
        _get_call_session_or_404(session, context.tenant_id, call_session_id)
        rows = (
            session.execute(
                select(ConversationTurn)
                .where(ConversationTurn.tenant_id == context.tenant_id)
                .where(ConversationTurn.call_session_id == call_session_id)
                .order_by(ConversationTurn.sequence.asc())
            )
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows


def delete_conversation_turns(context: TenantContext, call_session_id: uuid.UUID) -> int:
    """Delete every durable conversation turn for one call (Phase 2.5 brief
    section 10: "deletable through an application service"). Scoped by both
    `tenant_id` and `call_session_id` in the `DELETE` itself -- RLS is a
    second, independent enforcement of the same boundary, never the only
    one. Returns the number of rows deleted (0 for a call with no
    conversation yet -- not an error). Raises `CallSessionNotFoundError` for
    a foreign or nonexistent call, matching `list_conversation_turns()`.

    A `CallSession` row itself is never deleted by this function or any
    other in this codebase -- no orphaned turn can result from this call,
    and the composite foreign key's `ON DELETE CASCADE`
    (`migrations/versions/0003_create_conversation_turns.py`) is documented
    defense for the day a future phase does add call-session deletion.
    """
    with tenant_scope(context) as session:
        _get_call_session_or_404(session, context.tenant_id, call_session_id)
        existing_ids = (
            session.execute(
                select(ConversationTurn.id)
                .where(ConversationTurn.tenant_id == context.tenant_id)
                .where(ConversationTurn.call_session_id == call_session_id)
            )
            .scalars()
            .all()
        )
        if not existing_ids:
            return 0
        session.execute(
            delete(ConversationTurn)
            .where(ConversationTurn.tenant_id == context.tenant_id)
            .where(ConversationTurn.call_session_id == call_session_id)
        )
        return len(existing_ids)


def _get_call_session_or_404(session, tenant_id: uuid.UUID, call_session_id: uuid.UUID) -> None:
    row = session.get(CallSession, call_session_id)
    if row is None or row.tenant_id != tenant_id:
        raise CallSessionNotFoundError(call_session_id)
