"""Observability seam (Phase 0 report section 17).

SaaS-OS owns the machinery: `infra.observability` configures structured
logging and OpenTelemetry tracing (both called by the platform lifespan),
provides correlation-context binding, and provides the redaction helpers that
keep sensitive values out of logs. The product does not reimplement any of it.

This module exists to give the product one import site for that machinery, and
to record -- next to the code -- the two logging rules this product cannot
afford to get wrong:

* **Never log call content.** No transcript text, no audio bytes, no caller
  identity. A debug line containing call content is a privacy incident, not a
  debugging convenience. `redact()`/`is_sensitive_key()` exist for this.
* **Correlate by call.** From Phase 2 the correlation id for everything a call
  touches is its `call_session_id`, bound once and carried into telephony
  commands, provider requests, tool executions and background jobs.

Phase 1 instruments only what exists: HTTP requests already carry a
correlation id through the platform's middleware. No call-session traces, no
provider-latency metrics, and no placeholder metrics for behavior that has not
been built -- a fake metric is worse than none, because it looks like signal.
"""

from __future__ import annotations

from infra.observability import (
    bind_correlation_context,
    get_correlation_context,
    get_tracer,
    is_sensitive_key,
    redact,
)

__all__ = [
    "bind_correlation_context",
    "get_correlation_context",
    "get_tracer",
    "is_sensitive_key",
    "redact",
]
