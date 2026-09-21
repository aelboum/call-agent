# ADR-0009: Initial AI provider strategy

Status: Accepted (Phase 2.0)
Date: 2026-09-21
Resolves: Phase 0 report §19 OD-3

## Context

ADR-0006 fixed the shape — `ConversationEngine` with `PipelinedEngine`
(composed `SttProvider`/`LlmProvider`/`TtsProvider`) and a product-owned
`RealtimeEngine` — without choosing a first concrete provider for either.
Phase 2.1 needs one vertical slice to build against. This ADR chooses it,
without letting that choice become a hard architectural dependency: no
provider name may appear in `voiceagent.providers.engines.contracts`, and the
exit criteria in ADR-0006 apply here exactly as they apply to Pipecat.

### What was researched (facts, sources, dates — full detail in the Phase 2.0
report §22; this ADR states only the conclusions that bear on the decision)

- **OpenAI Realtime (`gpt-realtime`)** is a true duplex speech-to-speech
  session: one WebSocket, server-side VAD/turn-detection, native function
  calling, native PCM16 (24 kHz, mono, little-endian) *and* native
  `g711_ulaw`/`g711_alaw` output — the latter is the standard 8 kHz telephony
  encoding, so a `RealtimeEngine` adapter can request telephony-native audio
  directly rather than resampling. Zero Data Retention is available to
  eligible API customers; enterprise API data is not used for training by
  default. (developers.openai.com/api/docs/guides/realtime-conversations;
  developers.openai.com/api/docs/models/gpt-realtime; openai.com/enterprise-privacy;
  checked 2026-09-21.)
- **ElevenLabs Agents (formerly Conversational AI)** is an *orchestrated*
  platform — ElevenLabs runs STT, an LLM of the customer's choice, TTS and a
  proprietary turn-taking model behind one session API, not a raw model
  endpoint. 30+ languages including Dutch and Arabic are listed; native
  telephony integration (including Twilio) exists; tool/function calling is
  supported; Zero Retention Mode is available per-request
  (`enable_logging=false`) for Text-to-Speech, Speech-to-Text and Agents
  specifically, and Enterprise accounts do not train on customer data by
  default. (elevenlabs.io/docs/eleven-agents/overview;
  elevenlabs.io/docs/eleven-api/resources/zero-retention-mode;
  help.elevenlabs.io "Which languages can I use with ElevenLabs Agents";
  checked 2026-09-21.)
- **Deepgram (Nova-3)** streaming STT: 45+ languages including Dutch,
  streaming and batch from the same model, competitive per-minute pricing
  (low single-digit cents). (deepgram.com/pricing; deepgram.com/learn/nova-2-...;
  checked 2026-09-21.)
- **Darija (Moroccan Arabic) is not documented as a distinct, verified
  language by any provider researched.** Every vendor's language list names
  "Arabic" as a single entry (implicitly Modern Standard Arabic and/or major
  dialects); none publishes a Darija-specific word-error-rate or evaluation.
  This is a real, unresolved risk for a product whose reference context
  (SaaS-OS's own ADR-0015 names MarocAssist, AuraVox as example consuming
  projects) includes Moroccan Arabic callers, and it cannot be resolved by
  reading documentation — it requires evaluation against real Darija audio
  samples before any provider commitment is made for that market. Recorded as
  an open question (Phase 2.0 report §20), not decided here.

## Decision

1. **The first implementation vertical slice targets `PipelinedEngine`, not
   `RealtimeEngine`.** A pipelined engine's three components (STT, LLM, TTS)
   are independently swappable behind three separate, already-defined
   contracts (`SttProvider`, `LlmProvider`, `TtsProvider`); a realtime engine's
   single vendor session is not decomposable at all. Starting pipelined
   maximizes what Phase 2.1 learns about the `ConversationEngine` contract
   itself without over-committing to one vendor's session semantics on the
   very first slice.
2. **Development/integration-testing STT: a provider consistent with
   Deepgram's documented streaming shape** (partial + final transcripts over
   a persistent WebSocket, telephony-native 8 kHz input). Not a permanent
   commitment — chosen because streaming behavior is well-documented and
   inexpensive to integration-test against.
3. **Development/integration-testing LLM: any streaming, tool-calling-capable
   chat completion API** (the `LlmProvider` contract in
   `voiceagent.providers.engines.contracts` already specifies the shape:
   `stream_turn(messages, tools) -> AsyncIterator[str | ToolCallRequested |
   TurnEnded]`). This ADR deliberately does not name a specific LLM vendor:
   the contract is the smallest, most commodity-like of the three, and naming
   one here would create exactly the false permanence this ADR exists to
   avoid.
4. **Development/integration-testing TTS: a provider consistent with
   ElevenLabs' documented streaming-synthesis shape**, used *only* through the
   `TtsProvider` contract inside `PipelinedEngine` for this slice — not
   through ElevenLabs Agents' own orchestrated session, which would be a
   `RealtimeEngine`-shaped integration and is explicitly deferred (point 6).
5. **`RealtimeEngine`'s first candidate, when Phase 2.1's follow-on work
   builds it, is OpenAI Realtime** — on the strength of its native
   `g711_ulaw` telephony output (no resampling adapter needed on that leg) and
   documented Zero Data Retention eligibility. This is a candidate for the
   *second* engine implementation (ADR-0006's own requirement: a working
   pipelined engine and a working realtime engine, from two different
   vendors, before the `ConversationEngine` abstraction is declared proven),
   not a Phase 2.1 deliverable.
6. **None of the three researched vendors is a hard architectural
   dependency.** No vendor SDK, vendor type, or vendor-specific field may
   appear in `voiceagent.providers.engines.contracts` (ADR-0006, unchanged).
   Each vendor is confined to its own adapter module beneath
   `voiceagent.providers.engines.` — none of which is built in Phase 2.0 or
   Phase 2.1 (this ADR is a decision document, not an implementation).
   **ElevenLabs Agents specifically must never become the
   `ConversationEngine` abstraction's assumed shape**: it is an
   orchestrated-session product, and if its session model were allowed to
   leak into the contract, swapping to OpenAI Realtime or to a fully
   product-composed pipeline later would require exactly the rewrite ADR-0006
   exists to prevent.
7. **Provider eligibility and data-training configuration are policy, not
   code.** SaaS-OS's `control_plane.data_authorization` module already models
   this precisely: `ProviderEligibilityPolicy.eligible_providers`,
   `TenantAIDataPolicy.allowed_providers`, and
   `DataAuthorizationRequest.provider` as a plain string. No new mechanism is
   invented; Phase 2.1's `authorize_data_access()` call site (Phase 2.0 report
   §13) supplies the selected provider's identifier as that string, and a
   tenant's or the platform's policy governs whether it is eligible — see
   point 8 for what "not training" concretely obligates an adapter to do.
8. **"Configured not to train" is an adapter-level, per-call obligation, not
   an assumption.** For a provider offering a per-request opt-out (ElevenLabs'
   `enable_logging=false`), the adapter must set it on every request with no
   code path that omits it. For a provider offering account-level Zero Data
   Retention (OpenAI, subject to eligibility), the adapter's authorization
   layer must verify the deployment's account is actually enrolled before
   treating that provider as eligible for `sensitive_pii`-classified
   `DataAuthorizationRequest`s. Neither obligation is discharged by this ADR;
   both are recorded as Phase 2.1+ adapter requirements, enforced at the
   `authorize_data_access()` gate (Phase 2.0 report §13), never left to a
   default.

## Rejected alternatives

- **Starting with `RealtimeEngine` first** — rejected: it commits the first
  vertical slice to one vendor's indivisible session shape, the opposite of
  what a first slice should stress-test the contract against.
- **Naming a single LLM vendor now** — rejected: the `LlmProvider` contract is
  the most commodity-shaped of the three surfaces, and this platform gains
  nothing architecturally from an early commitment there.
- **Adopting ElevenLabs Agents as the realtime candidate instead of OpenAI
  Realtime** — rejected for the *first* realtime candidate specifically
  because its orchestrated-session shape is the harder one to keep the
  contract's abstraction honest against; it remains a legitimate second or
  third `RealtimeEngine` adapter once the contract has already been proven
  against a more primitive session.
- **Deferring any provider decision until Phase 2.1 is underway** — rejected:
  Phase 2.1's engine-conformance and integration tests need a concrete slice
  to build the first adapter against, and the brief for this phase asks for a
  specific recommendation, not an open menu.

## Consequences

Positive: Phase 2.1 has an unambiguous first target with documented technical
grounding; the realtime candidate is chosen for a concrete, verifiable
architectural reason (native telephony audio encoding) rather than brand
preference; the data-training obligation is tied to the exact mechanism
SaaS-OS already provides, so no parallel policy system is invented.

Negative and accepted: Darija support remains genuinely unresolved and must be
evaluated empirically before any Moroccan-market commitment; ElevenLabs
Agents, though researched and documented, is deliberately not the first
`RealtimeEngine` target, deferring a specific product need (voice quality
provenance across the two candidates) to a later comparison once both
adapters exist.

## What would be difficult to change later

Nothing in this ADR is hard to change — that is its point. The one property
that must not erode is point 6: the moment a vendor-specific concept appears
in `voiceagent.providers.engines.contracts`, this ADR's entire "not a hard
dependency" framing becomes retroactively false for every call already made
against it.

## What is deliberately not decided here

The exact LLM vendor (point 3); whether the first `PipelinedEngine` slice uses
Deepgram/ElevenLabs by their real names or a functionally-equivalent
alternative discovered during Phase 2.1 implementation (this ADR names the
*documented shape* it is building against, not a signed vendor contract);
pricing/cost-attribution modeling (Phase 0 report §17 observability, R-9);
Darija evaluation methodology and pass/fail criteria; whether OpenAI Realtime
remains the sole `RealtimeEngine` candidate once ElevenLabs Agents is
evaluated as a second one.

## Related

ADR-0006 (`ConversationEngine` implementation strategy, the abstraction this
ADR fills in without altering); SaaS-OS `control_plane.data_authorization`
(the provider-eligibility mechanism reused unmodified); SaaS-OS ADR-0013
(external model boundary); Phase 2.0 report §4 (provider decision detail),
§13 (privacy authorization point), §22 (full research citations).
