"""Product error seam.

SaaS-OS's `api.errors` already provides HTTP error responses that are stable
and non-leaking -- a fixed `detail` string per class, never an exception
message, never a stack trace, never an internal identifier. That is the
product's error foundation too; this module re-exports it so product routes
have one import site and a future upstream change is a one-file fix, exactly
like `voiceagent.db`.

Phase 2.1 adds `conflict()` -- SaaS-OS's own `api.errors` has no 409 helper
because it has never needed one; this product does, specifically for the
generic phone-number-unavailable response (Phase 2.1 brief §12/§24) and for
an invalid AgentVersion/CallSession lifecycle transition. It is product-owned
(not an upstream addition) but follows `api.errors`' own established shape
exactly: a fixed `detail` string, never an exception message.

Domain errors (`voiceagent.agents.errors`, `voiceagent.phone_numbers.errors`,
`voiceagent.calls.errors`) are mapped onto these responses only at the route
boundary (`voiceagent.api.v1.*`) -- the mapping stays the place where
internal detail stops.

Two invariants inherited from `api.platform.build_platform_app()` and not to
be undone: the application is constructed with `debug=False` unconditionally,
and there is no configuration flag that turns it on.
"""

from __future__ import annotations

from api.errors import (
    forbidden,
    not_found,
    quota_exceeded,
    rate_limited,
    unauthorized,
)
from fastapi import HTTPException, status

__all__ = [
    "conflict",
    "forbidden",
    "not_found",
    "quota_exceeded",
    "rate_limited",
    "unauthorized",
]


def conflict(detail: str = "Conflict.") -> HTTPException:
    """A 409. `detail` must never carry data from another tenant's row or a
    raw database exception -- every call site in this codebase passes a
    fixed, reviewed string (see `voiceagent.phone_numbers.errors.
    PhoneNumberUnavailableError` for the one place this matters most)."""
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
