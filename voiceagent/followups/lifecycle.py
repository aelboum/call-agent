"""The `FollowUpAction` status state machine (Phase 2.7 brief §7/§21;
extended by Phase 2.9 brief §3 for safe scheduled execution).

```text
pending    -> processing | completed | cancelled
processing -> completed | failed
failed     -> processing | cancelled
completed  -> (terminal; no further transition)
cancelled  -> (terminal; no further transition)
```

`pending -> completed` and `pending -> cancelled` are Phase 2.7's own
transitions, unchanged -- `voiceagent.followups.service.complete_follow_up()`/
`cancel_follow_up()` still reach them directly for a follow-up type with no
concrete execution service (brief §5: `'contact'`/`'manual_follow_up'` are
closed out by a human, never by `claim_due_follow_up()`).

`processing -> completed | failed` is `voiceagent.followups.service
.complete_follow_up_execution()`/`fail_follow_up_execution()` (brief §6/§8).
`failed -> processing` is a reclaim -- either an automatic retry
(`claim_due_follow_up()`, once `next_attempt_at`'s backoff has elapsed) or an
administratively requested one (`reprocess_follow_up()`, brief §12/§13).
`failed -> cancelled` is `cancel_follow_up()` giving up on a failed
follow-up before its retries are exhausted -- deliberately kept legal here
(brief §4: "a cancelled follow-up is never executable") rather than forcing
an operator to wait out `retry_policy.MAX_ATTEMPTS`.

Deliberately a plain lookup table, not a generic state-machine framework --
mirrors `voiceagent.calls.lifecycle`'s own "simplest explicit state
transition mechanism" discipline, pure and DB-free so it is hermetically
unit-tested on its own. A redelivered transition to the *current* status
(including a terminal one, or `processing -> processing` -- a stale-lease
reclaim, brief §9) is a no-op, not an error -- the same "duplicate events
must not corrupt state" rule `voiceagent.calls.lifecycle` already
establishes.

**`TERMINAL_STATUSES` is deliberately unchanged from Phase 2.7**
(`{"completed", "cancelled"}`) -- not reused blindly, but re-derived: it is
still exactly the set of statuses with no outgoing edge in `_TRANSITIONS`
below. `"processing"` and `"failed"` are not terminal -- both have at least
one legal outgoing transition -- even though a `"failed"` row that has
exhausted `retry_policy.MAX_ATTEMPTS` is *practically* terminal (never
claimed again). That exhaustion is retry-policy/attempt-count bookkeeping,
not a lifecycle concept, so it is not modeled as a distinct status here --
see `voiceagent.followups.retry_policy`.
"""

from __future__ import annotations

__all__ = ["TERMINAL_STATUSES", "VALID_STATUSES", "is_valid_follow_up_transition"]

VALID_STATUSES = frozenset({"pending", "processing", "completed", "cancelled", "failed"})

TERMINAL_STATUSES = frozenset({"completed", "cancelled"})

_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"processing", "completed", "cancelled"}),
    "processing": frozenset({"completed", "failed"}),
    "failed": frozenset({"processing", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


def is_valid_follow_up_transition(current: str, target: str) -> bool:
    """`True` if `current -> target` is a legal transition, or if `target`
    is a genuine no-op re-delivery of `current` (including a terminal
    status); `False` otherwise -- never raises on an unrecognized status
    string, matching `voiceagent.calls.lifecycle.is_valid_transition()`'s
    own contract."""
    if current == target:
        return True
    return target in _TRANSITIONS.get(current, frozenset())
