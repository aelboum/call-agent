"""The AI call runtime -- Phase 2, deliberately empty in Phase 1.

This package will own the per-call turn loop: audio in and out, barge-in,
`ConversationEngine` orchestration, transcript assembly, tool-call requests
and timers (Phase 0 report section 5). Nothing of it exists yet.

Two boundaries are already fixed and must hold when it is written:

* It never accesses the database directly and never constructs its own tenant
  context -- it receives a verified `voiceagent.tenancy.TenantContext` and
  reaches data only through the Tool Gateway (ADR-0003).
* It never imports a provider SDK, Pipecat, or anything under
  `voiceagent.telephony.freeswitch`; it depends on the `ConversationEngine`,
  `TelephonyProvider` and `MediaProvider` contracts.

The package exists in Phase 1 so those boundaries have a home and the
architecture tests have something to assert against.
"""

from __future__ import annotations

__all__: list[str] = []
