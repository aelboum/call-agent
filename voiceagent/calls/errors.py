"""Domain errors for `CallSession`."""

from __future__ import annotations

__all__ = [
    "CallSessionAlreadyOwnedError",
    "CallSessionError",
    "CallSessionNotFoundError",
    "CallSessionOwnershipMismatchError",
    "InvalidCallSessionTransitionError",
]

# Phase 2.6 (brief §4) reuses `voiceagent.contacts.errors.ContactNotFoundError`
# for "no such contact in this tenant" -- both when a contact_id is looked up
# directly and when `associate_call()`'s contact_id belongs to no row this
# tenant can see (including a cross-tenant id) -- rather than adding a second
# not-found error class for the identical condition.


class CallSessionError(Exception):
    """Base class for every CallSession domain error."""


class CallSessionNotFoundError(CallSessionError):
    def __init__(self, call_session_id: object) -> None:
        super().__init__(f"CallSession not found: {call_session_id}")


class InvalidCallSessionTransitionError(CallSessionError):
    def __init__(self, call_session_id: object, current: str, target: str) -> None:
        super().__init__(
            f"CallSession {call_session_id} cannot transition {current!r} -> {target!r}"
        )


class CallSessionAlreadyOwnedError(CallSessionError):
    """Raised by `voiceagent.calls.service.claim_runtime_ownership()` when a
    call is already owned by a *different* runtime instance (ADR-0008 point
    10: ownership is exclusive by construction -- a second runtime must never
    silently take over)."""

    def __init__(self, call_session_id: object, owning_runtime_instance_id: str) -> None:
        super().__init__(
            f"CallSession {call_session_id} is already owned by runtime "
            f"{owning_runtime_instance_id!r}"
        )
        self.owning_runtime_instance_id = owning_runtime_instance_id


class CallSessionOwnershipMismatchError(CallSessionError):
    """Raised by `voiceagent.calls.service.transition_call_session()` when a
    caller supplies `expected_runtime_instance_id` and that does not match
    the call's own `runtime_instance_id` (Phase 2.13 brief §15/§20: "only the
    owning runtime may operate a call"; "do not trust ... without validating
    call/session ownership"). Distinct from `CallSessionAlreadyOwnedError`,
    which `claim_runtime_ownership()` raises for a *conflicting claim*; this
    one is raised for a conflicting *operation* -- a runtime attempting to
    progress a call's lifecycle it does not (or no longer) hold."""

    def __init__(
        self,
        call_session_id: object,
        *,
        expected_runtime_instance_id: str,
        actual_runtime_instance_id: str | None,
    ) -> None:
        super().__init__(
            f"CallSession {call_session_id} is owned by runtime "
            f"{actual_runtime_instance_id!r}, not {expected_runtime_instance_id!r}"
        )
        self.expected_runtime_instance_id = expected_runtime_instance_id
        self.actual_runtime_instance_id = actual_runtime_instance_id
