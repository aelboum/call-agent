"""Operational error categorization (Phase 2.14 brief section 9).

**Reuses every existing error hierarchy; invents nothing new.** This module
does not redesign `voiceagent.calls.errors`/`voiceagent.runtime.errors`/
`voiceagent.tools.errors`/`voiceagent.workflows.errors`/
`voiceagent.providers.engines.contracts.EngineErrorCode` -- it only maps each
one, by `isinstance` check, onto a small, bounded *operational* category
string, so a metric label or a log field can carry "what kind of failure was
this" without listing every domain exception class it might ever see (brief:
"the purpose is operational classification, not redesigning all errors").

A category is chosen by the *first* matching rule below, most specific first
(a provider timeout is `"timeout"` before it is `"transient"`-shaped; an
authorization denial is `"privacy_denied"`/`"tenant_isolation"` before it is
generically `"internal"`). An `EngineException` deliberately keeps its own
`EngineErrorCode` value as the category (`"auth"`, `"rate_limit"`,
`"transient"`, `"invalid_request"`, `"provider_down"`) rather than folding it
into this module's own list -- that taxonomy already exists, already has
every STT/LLM/TTS adapter mapping onto it, and this phase's brief is explicit
that reusing it is the point.

An exception this module has never seen before (a bug, a vendor library leak,
a genuine unknown) categorizes as `"internal"` -- never raises, and never
`None`. `categorize_exception()` is the one place a caller asks "what
operational bucket does this failure belong to"; nothing here inspects
exception *messages* (which can carry customer content) beyond the module
name of a redis/database client library's own base error class.
"""

from __future__ import annotations

import asyncio

from voiceagent.calls.errors import (
    CallSessionOwnershipMismatchError,
    InvalidCallSessionTransitionError,
)
from voiceagent.providers.engines.contracts import EngineException
from voiceagent.runtime.errors import CallCancelledError, DataAuthorizationDeniedError
from voiceagent.telephony.contracts import TelephonyError
from voiceagent.tools.errors import ToolError
from voiceagent.workflows.errors import WorkflowError

__all__ = ["ErrorCategory", "categorize_exception"]

#: The bounded set this module ever returns. Not an exhaustive claim about
#: every possible failure in this product -- `"internal"` is the deliberate
#: catch-all for anything not explicitly recognized below.
ErrorCategory = str

_REDIS_MODULE_PREFIXES = ("redis",)
_DATABASE_MODULE_PREFIXES = ("sqlalchemy", "psycopg", "psycopg2")


def categorize_exception(exc: BaseException) -> ErrorCategory:
    if isinstance(exc, asyncio.CancelledError | CallCancelledError):
        return "cancellation"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, DataAuthorizationDeniedError):
        return "privacy_denied"
    if isinstance(exc, CallSessionOwnershipMismatchError):
        return "tenant_isolation"
    if isinstance(exc, InvalidCallSessionTransitionError):
        return "validation"
    if isinstance(exc, TelephonyError):
        return "telephony"
    if isinstance(exc, ToolError):
        return "tool"
    if isinstance(exc, WorkflowError):
        return "workflow"
    if isinstance(exc, EngineException):
        # Reuse EngineErrorCode as-is (module docstring) -- never remapped
        # onto this module's own category names.
        return exc.code.value

    module = type(exc).__module__
    if module.startswith(_REDIS_MODULE_PREFIXES):
        return "redis"
    if module.startswith(_DATABASE_MODULE_PREFIXES):
        return "database"

    return "internal"
