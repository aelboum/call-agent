"""Application service for `CallAnalysis` (Phase 2.8 brief §5, §10).

**Authoritative source domains** -- `CallAnalysis` derives every field
below from these, and owns none of them:

```text
metric                          authoritative source
------------------------------  --------------------------------------------
turn_count/user_turn_count/...  voiceagent.conversations.models.ConversationTurn
duration_ms                     voiceagent.calls.models.CallSession.duration_ms
had_transfer/had_hold           ConversationTurn (role='tool_call', tool_payload)
contact_associated              voiceagent.calls.models.CallSession.contact_id
outcome                         voiceagent.followups.models.CallOutcome.outcome
follow_up_count/...             voiceagent.followups.models.FollowUpAction
```

**Metric rules (brief §10), stated exactly, with no semantic inference**:

- `turn_count` = number of persisted `ConversationTurn` rows for the call
  (every role, including `system`).
- `user_turn_count`/`assistant_turn_count`/`tool_call_count`/
  `tool_result_count` = the same, filtered to `role in
  ('user', 'assistant', 'tool_call', 'tool_result')` respectively.
- `had_transfer`/`had_hold` = `True` iff at least one `tool_call` turn's
  `tool_payload["name"]` equals `"call.transfer"`/`"call.hold"` -- the exact
  tool id `voiceagent.tools.handlers` registers those two tools under.
  Never derived from transcript text.
- `contact_associated` = `CallSession.contact_id IS NOT NULL`.
- `outcome` = `CallOutcome.outcome` for this call if one exists, else
  `None` -- never invented, never defaulted to a sentinel string.
- `follow_up_count` = every `FollowUpAction` row for the call.
  `appointment_follow_up_count` = the subset with `type == 'appointment'`.
  `open_follow_up_count` = the subset whose `status` is not in
  `voiceagent.followups.lifecycle.TERMINAL_STATUSES` (i.e. still
  `'pending'`) -- reusing that module's own lifecycle definition, never a
  second one.
- `duration_ms` = `CallSession.duration_ms` verbatim (already `None` unless
  both `started_at`/`ended_at` were set -- see
  `voiceagent.calls.service`'s own module docstring) -- never recomputed
  here, never invented for a call that has not reached a terminal status.

**No AI provider call, no network dependency, no direct database access
from the Tool Gateway or an AI engine** -- this module is a plain,
synchronous application service reached only through
`voiceagent.tenancy.tenant_scope()`, exactly like every other
`voiceagent.*.service` module.

**Idempotent rebuild**: `build_call_analysis()` is an upsert -- a second
call for the same `call_session_id` updates the existing row's derived
values in place rather than creating a duplicate (`uq_call_analysis_call_session`
also enforces this at the database layer). The whole computation and
persistence happens inside one `tenant_scope()` session/transaction, so a
rebuild is atomic: either every derived field advances together, or none
does.
"""

from __future__ import annotations

import uuid

from voiceagent.call_analysis.errors import CallAnalysisNotFoundError
from voiceagent.call_analysis.models import CallAnalysis
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.calls.models import CallSession
from voiceagent.conversations.models import ConversationTurn
from voiceagent.db import select
from voiceagent.followups.lifecycle import TERMINAL_STATUSES
from voiceagent.followups.models import CallOutcome, FollowUpAction
from voiceagent.tenancy import TenantContext, tenant_scope

__all__ = ["build_call_analysis", "get_call_analysis"]

_TRANSFER_TOOL_ID = "call.transfer"
_HOLD_TOOL_ID = "call.hold"


def _require_call_row(session, tenant_id: uuid.UUID, call_session_id: uuid.UUID) -> CallSession:
    row = session.get(CallSession, call_session_id)
    if row is None or row.tenant_id != tenant_id:
        raise CallSessionNotFoundError(call_session_id)
    return row


def _find_analysis_row(
    session, tenant_id: uuid.UUID, call_session_id: uuid.UUID
) -> CallAnalysis | None:
    return (
        session.execute(
            select(CallAnalysis)
            .where(CallAnalysis.tenant_id == tenant_id)
            .where(CallAnalysis.call_session_id == call_session_id)
        )
        .scalars()
        .first()
    )


def _tool_name(turn: ConversationTurn) -> str | None:
    if turn.tool_payload is None:
        return None
    name = turn.tool_payload.get("name")
    return str(name) if name is not None else None


def build_call_analysis(context: TenantContext, call_session_id: uuid.UUID) -> CallAnalysis:
    """Compute and persist (or refresh) the analysis for one call from the
    authoritative persisted records -- tenant scoped, deterministic, and
    safe to call more than once (see module docstring). Raises
    `CallSessionNotFoundError` if `call_session_id` does not belong to
    `context.tenant_id`, matching every other lookup in this codebase."""
    with tenant_scope(context) as session:
        call = _require_call_row(session, context.tenant_id, call_session_id)

        turns = (
            session.execute(
                select(ConversationTurn)
                .where(ConversationTurn.tenant_id == context.tenant_id)
                .where(ConversationTurn.call_session_id == call_session_id)
            )
            .scalars()
            .all()
        )
        turn_count = len(turns)
        user_turn_count = sum(1 for turn in turns if turn.role == "user")
        assistant_turn_count = sum(1 for turn in turns if turn.role == "assistant")
        tool_call_count = sum(1 for turn in turns if turn.role == "tool_call")
        tool_result_count = sum(1 for turn in turns if turn.role == "tool_result")
        had_transfer = any(
            turn.role == "tool_call" and _tool_name(turn) == _TRANSFER_TOOL_ID for turn in turns
        )
        had_hold = any(
            turn.role == "tool_call" and _tool_name(turn) == _HOLD_TOOL_ID for turn in turns
        )

        outcome_row = (
            session.execute(
                select(CallOutcome)
                .where(CallOutcome.tenant_id == context.tenant_id)
                .where(CallOutcome.call_session_id == call_session_id)
            )
            .scalars()
            .first()
        )

        follow_ups = (
            session.execute(
                select(FollowUpAction)
                .where(FollowUpAction.tenant_id == context.tenant_id)
                .where(FollowUpAction.call_session_id == call_session_id)
            )
            .scalars()
            .all()
        )
        follow_up_count = len(follow_ups)
        appointment_follow_up_count = sum(1 for f in follow_ups if f.type == "appointment")
        open_follow_up_count = sum(1 for f in follow_ups if f.status not in TERMINAL_STATUSES)

        values: dict[str, object] = {
            "status": "ready",
            "turn_count": turn_count,
            "user_turn_count": user_turn_count,
            "assistant_turn_count": assistant_turn_count,
            "tool_call_count": tool_call_count,
            "tool_result_count": tool_result_count,
            "duration_ms": call.duration_ms,
            "had_transfer": had_transfer,
            "had_hold": had_hold,
            "contact_associated": call.contact_id is not None,
            "outcome": outcome_row.outcome if outcome_row is not None else None,
            "follow_up_count": follow_up_count,
            "appointment_follow_up_count": appointment_follow_up_count,
            "open_follow_up_count": open_follow_up_count,
        }

        existing = _find_analysis_row(session, context.tenant_id, call_session_id)
        if existing is None:
            row = CallAnalysis(
                tenant_id=context.tenant_id, call_session_id=call_session_id, **values
            )
            session.add(row)
        else:
            for key, value in values.items():
                setattr(existing, key, value)
            row = existing

        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def get_call_analysis(context: TenantContext, call_session_id: uuid.UUID) -> CallAnalysis:
    """Read-only. Raises `CallAnalysisNotFoundError` when no analysis has
    been built yet for this call -- never triggers a build itself (brief
    §7: "the default API should primarily be read-oriented")."""
    with tenant_scope(context) as session:
        row = _find_analysis_row(session, context.tenant_id, call_session_id)
        if row is None:
            raise CallAnalysisNotFoundError(call_session_id)
        session.expunge(row)
        return row
