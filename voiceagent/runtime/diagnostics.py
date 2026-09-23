"""Runtime diagnostics (Phase 2.14 brief section 12): a safe, in-process
operational snapshot of one `CallRuntime`.

A plain function over `CallRuntime`'s own already-public, content-free
surface (`instance_id`, `current_load`, `capacity`, `is_shutting_down`,
`owned_call_session_ids`) -- never a new introspection API added to
`CallRuntime` itself, and never a live HTTP endpoint reaching across the
call-runtime/API process boundary ADR-0008 establishes: a `CallRuntime` runs
in its own process (ADR-0008 point 1), with no HTTP surface of its own in
this phase -- no entrypoint script exists yet for either process
(`scripts/` holds only `bootstrap_rbac.py`), so nothing in `voiceagent.api`
can introspect a live `CallRuntime` object directly. This function is the
seam a future call-runtime entrypoint calls to report on itself (a local
health/metrics port, a periodic structured log line, or a future operator
channel) -- Phase 2.14 does not invent that channel, which would be exactly
the kind of new cross-process mechanism the brief warns against (section 25:
no new distributed tracing backend, no generic event bus).

What the API process *can* safely report today, cross-process, is covered
separately by `voiceagent.api.v1.ops`: the Redis heartbeat set
(`voiceagent.runtime.heartbeat.HeartbeatStore.read_all()`, by design already
readable from any process, since the Call Orchestrator itself reads it to
pick a runtime -- ADR-0008 point 8) and a DB-based stuck-call scan
(`voiceagent.runtime.stuck_calls`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from voiceagent.runtime.supervisor import CallRuntime

__all__ = ["RuntimeDiagnostics", "build_runtime_diagnostics"]


@dataclass(frozen=True, slots=True)
class RuntimeDiagnostics:
    """Identifiers, counts and state only -- never call content (brief
    section 12: "Do not expose transcripts, prompts, provider credentials,
    customer data")."""

    instance_id: str
    capacity: int
    current_load: int
    is_shutting_down: bool
    owned_call_session_ids: tuple[uuid.UUID, ...]

    @property
    def has_capacity(self) -> bool:
        return self.current_load < self.capacity


def build_runtime_diagnostics(runtime: CallRuntime) -> RuntimeDiagnostics:
    """A snapshot, not a live view -- callers that need a fresh one call
    this again. Cheap and synchronous: reads five already-computed
    attributes, no I/O."""
    return RuntimeDiagnostics(
        instance_id=runtime.instance_id,
        capacity=runtime.capacity,
        current_load=runtime.current_load,
        is_shutting_down=runtime.is_shutting_down,
        owned_call_session_ids=runtime.owned_call_session_ids,
    )
