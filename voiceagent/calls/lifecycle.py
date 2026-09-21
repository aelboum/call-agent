"""The `CallSession` lifecycle state machine (Phase 2.0 report §11.6, §17).

```text
initiated -> ringing | answered | failed | interrupted
ringing   -> answered | failed | interrupted
answered  -> in_progress | completed | failed | interrupted
in_progress -> completed | failed | interrupted
completed | failed | interrupted -> (terminal; no further transition)
```

Deliberately a plain lookup table, not a generic state-machine framework
(Phase 2.1 brief §14/§17: "use the simplest explicit state transition
mechanism"; "do not over-engineer a generic state-machine framework"). Pure
and DB-free, so it is hermetically unit-tested on its own (`tests/calls/
test_lifecycle.py`) independent of anything that needs a live database.

**Duplicate lifecycle events must not corrupt state** (Phase 2.0 report §16/
§17): re-delivering the event that produced the call's *current* status is
defined as a no-op, not an error -- `is_valid_transition(status, status)` is
`True` for every status, including terminal ones, so a redelivered
`CHANNEL_HANGUP` after a call is already `completed` does not raise.
**Delayed events that contradict an already-terminal state must not reopen
it** -- attempting to move a terminal status to a *different* status is
rejected.
"""

from __future__ import annotations

__all__ = ["TERMINAL_STATUSES", "VALID_STATUSES", "is_valid_transition"]

VALID_STATUSES = frozenset(
    {"initiated", "ringing", "answered", "in_progress", "completed", "failed", "interrupted"}
)

TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})

_TRANSITIONS: dict[str, frozenset[str]] = {
    "initiated": frozenset({"ringing", "answered", "failed", "interrupted"}),
    "ringing": frozenset({"answered", "failed", "interrupted"}),
    "answered": frozenset({"in_progress", "completed", "failed", "interrupted"}),
    "in_progress": frozenset({"completed", "failed", "interrupted"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "interrupted": frozenset(),
}


def is_valid_transition(current: str, target: str) -> bool:
    """`True` if `current -> target` is a legal transition, if `target` is a
    genuine no-op re-delivery of `current` (including a terminal status), or
    `False` otherwise -- never raises on an unrecognized status string; the
    caller (`voiceagent.calls.service.transition_call_session`) is
    responsible for validating that both values are members of
    `VALID_STATUSES` at all (a `CHECK` constraint enforces the same thing in
    the database, independently)."""
    if current == target:
        return True
    return target in _TRANSITIONS.get(current, frozenset())
