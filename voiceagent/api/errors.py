"""Product error seam.

SaaS-OS's `api.errors` already provides HTTP error responses that are stable
and non-leaking -- a fixed `detail` string per class, never an exception
message, never a stack trace, never an internal identifier. That is the
product's error foundation too; this module re-exports it so product routes
have one import site and a future upstream change is a one-file fix, exactly
like `voiceagent.db`.

There is deliberately no product exception hierarchy here in Phase 1: no
product domain exists to raise one. When domain errors arrive (Phase 2), they
map onto these responses at the route boundary, and the mapping stays the
place where internal detail stops.

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

__all__ = [
    "forbidden",
    "not_found",
    "quota_exceeded",
    "rate_limited",
    "unauthorized",
]
