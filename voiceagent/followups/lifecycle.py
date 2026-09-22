"""The `FollowUpAction` status state machine (Phase 2.7 brief §7/§21).

```text
pending -> completed | cancelled
completed | cancelled -> (terminal; no further transition)
```

Deliberately a plain lookup table, not a generic state-machine framework --
mirrors `voiceagent.calls.lifecycle`'s own "simplest explicit state
transition mechanism" discipline, pure and DB-free so it is hermetically
unit-tested on its own. A redelivered transition to the *current* status
(including a terminal one) is a no-op, not an error -- the same "duplicate
events must not corrupt state" rule `voiceagent.calls.lifecycle` already
establishes.
"""

from __future__ import annotations

__all__ = ["TERMINAL_STATUSES", "VALID_STATUSES", "is_valid_follow_up_transition"]

VALID_STATUSES = frozenset({"pending", "completed", "cancelled"})

TERMINAL_STATUSES = frozenset({"completed", "cancelled"})

_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"completed", "cancelled"}),
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
