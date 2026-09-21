"""External provider boundaries.

Every external service the product talks to sits behind a product-owned
contract in this package. The rule that makes the boundary real: **a vendor
SDK is imported only inside its own adapter module, never above it** -- the
same discipline SaaS-OS applies to Stripe inside `core/billing`.

Phase 1 contains contracts and fakes only. No vendor implementation exists,
and no vendor name appears in the domain:

* `engines/`      -- `ConversationEngine` (ADR-0006), and the `SttProvider`,
                     `LlmProvider`, `TtsProvider` and `VoiceCatalog`
                     protocols a `PipelinedEngine` will compose.
* `objectstore`   -- call-artifact storage (recordings, transcript exports).

Telephony and media providers deliberately live in `voiceagent.telephony`
instead: they are the telephony core's boundary (ADR-0002), not an
interchangeable external service.

Planned adapter locations, fixed now so the fences can exist before the code:
`engines/pipecat/` (the only place Pipecat may ever be imported, ADR-0006),
and one module per vendor beneath `engines/`.
"""

from __future__ import annotations

__all__: list[str] = []
