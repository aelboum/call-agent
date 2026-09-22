"""Vendor-neutral post-call intelligence provider abstraction (Phase 2.12).

Deliberately separate from `voiceagent.providers.llm` (Phase 2.3): that
registry builds a *streaming*, tool-call-capable `LlmProvider` for the live
`ConversationEngine` -- the wrong shape for a bounded, single-shot "analyze
this transcript, return one structured JSON object" call. This package is
the smaller abstraction Phase 2.12's brief asks for: one `analyze()` call,
one bounded response, no streaming, no tool calls, no live-call coupling.

Same isolation discipline as `voiceagent.providers.llm`: the domain layer
(`voiceagent.call_intelligence`) depends only on
`voiceagent.providers.call_intelligence.contracts.CallIntelligenceProvider`
and `voiceagent.providers.call_intelligence.registry
.create_call_intelligence_provider()` -- never on a vendor SDK, an `httpx`
response object, or a vendor's own JSON wire shape.
"""

from __future__ import annotations
