"""The AI call runtime (Phase 2.2).

Owns the per-call turn loop and the process that hosts many of them
concurrently (ADR-0008):

* `heartbeat.py` / `fakes.py` -- Redis-backed runtime liveness (ADR-0008
  point 8) and its deterministic double.
* `assignment.py` -- least-loaded runtime selection and the atomic ownership
  claim on `call_sessions` (ADR-0008 points 5, 10).
* `reconciliation.py` -- stale-ownership detection (ADR-0008 point 11); never
  session takeover (Phase 2.0 report §21 OQ-4 remains open).
* `db.py` -- the bounded `asyncio.to_thread`-style boundary every database
  touch from the call-hosting event loop must cross (ADR-0008 point 2).
* `privacy.py` -- the one-per-call `authorize_data_access()` gate (Phase 0
  report §16.5; Phase 2.0 report §14).
* `call_task.py` -- one call's full lifecycle: authorize, attach media, start
  the engine, pump audio/events, tear down.
* `supervisor.py` -- `CallRuntime`, one event loop's worth of concurrently
  supervised calls, each independently cancellable and error-isolated.

Two boundaries fixed since Phase 1 and unchanged here:

* It never accesses the database except through `voiceagent.runtime.db
  .DatabaseBoundary`, and never constructs its own tenant context -- every
  entrypoint receives a verified `voiceagent.tenancy.TenantContext`.
* It never imports a provider SDK, Pipecat, or anything under
  `voiceagent.telephony.freeswitch`; it depends on the `ConversationEngine`,
  `TelephonyProvider` and `MediaProvider` contracts.
"""

from __future__ import annotations

__all__: list[str] = []
