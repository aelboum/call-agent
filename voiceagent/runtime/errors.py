"""Domain errors for the call runtime (Phase 2.2)."""

from __future__ import annotations

__all__ = [
    "CallCancelledError",
    "DataAuthorizationDeniedError",
    "NoRuntimeCapacityError",
    "RuntimeError_",
]


class RuntimeError_(Exception):
    """Base class for every call-runtime domain error. Named with a trailing
    underscore only to avoid shadowing the builtin `RuntimeError` on the
    module's own `__all__` and at every import site."""


class NoRuntimeCapacityError(RuntimeError_):
    """Every known runtime is at or above its configured ceiling (ADR-0008
    points 6, 14: "no capacity is a defined outcome, not an unhandled
    case"). The caller (the Call Orchestrator) routes to the agent version's
    configured no-capacity fallback for inbound, or fails synchronously for
    outbound -- neither behavior is built in Phase 2.2 (no real FreeSWITCH
    wiring exists yet to route to), but the assignment step itself always
    produces this as a typed outcome rather than an unhandled exception or a
    silent wait for capacity to free up."""


class DataAuthorizationDeniedError(RuntimeError_):
    """`authorize_data_access()` denied this `CallSession` (Phase 0 report
    §16.5; Phase 2.0 report §14). Raised by `voiceagent.runtime.privacy` so
    the call task's own control flow cannot accidentally continue past a
    denial -- the engine is never started when this is raised."""

    def __init__(self, call_session_id: object, reason: str | None) -> None:
        super().__init__(f"CallSession {call_session_id} denied AI data access: {reason}")
        self.reason = reason


class CallCancelledError(RuntimeError_):
    """Raised inside a call task to unwind it cleanly on hangup, runtime
    shutdown, or an operator-issued cancellation -- distinguishes a
    deliberate stop from `asyncio.CancelledError`, which Python also raises
    for reasons the call task itself did not request (e.g. the process's own
    interpreter shutdown) and must still propagate unchanged."""

    def __init__(self, call_session_id: object, reason: str) -> None:
        super().__init__(f"CallSession {call_session_id} cancelled: {reason}")
        self.reason = reason
