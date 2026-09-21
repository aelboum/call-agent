"""External provider boundaries.

Every external service the product talks to sits behind a product-owned
contract in this package. The rule that makes the boundary real: **a vendor
SDK is imported only inside its own adapter module, never above it** -- the
same discipline SaaS-OS applies to Stripe inside `core/billing`.

* `engines/`      -- `ConversationEngine` (ADR-0006): `PipelinedEngine`,
                     `RealtimeEngine`, and `factory.build_conversation_engine()`,
                     the provider-selection seam (Phase 2.3).
* `stt/`, `llm/`, `tts/` -- one `ProviderRegistry` each
                     (`voiceagent.providers.registry`), and one module per
                     vendor. A vendor module (`stt/deepgram.py`,
                     `llm/gemini.py`, `tts/elevenlabs.py`, ...) is the only
                     place that vendor's request/response shape is ever
                     read or written; everything above a `registry.py`
                     depends only on `voiceagent.providers.engines.contracts`
                     protocols (`SttProvider`/`LlmProvider`/`TtsProvider`).
* `objectstore`   -- call-artifact storage (recordings, transcript exports).

Telephony and media providers deliberately live in `voiceagent.telephony`
instead: they are the telephony core's boundary (ADR-0002), not an
interchangeable external service.

`engines/pipecat/` remains the only place Pipecat may ever be imported
(ADR-0006) -- still not built; no engine implementation needs it.
"""

from __future__ import annotations

__all__: list[str] = []
