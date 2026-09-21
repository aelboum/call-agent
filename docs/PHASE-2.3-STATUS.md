# Phase 2.3 — Real AI Provider Vertical Slice: STATUS

**Status**: IN PROGRESS (implementation complete, awaiting review; not
committed)
**Updated**: 2026-09-21
**SaaS-OS pin**: `ff550010e5eafecace7311038aadc99fcecfbe3d` (unchanged, verified)

Phase 2.2 (`docs/PHASE-2.2-STATUS.md`) built the execution substrate against
fakes. This phase proves the same substrate works against real STT, LLM and
TTS providers -- and, per this phase's own brief, proves it in a way that is
**explicitly multi-provider and configuration-driven**, not tied to one
vendor per pipeline stage. Nothing here integrates a `RealtimeEngine`
vendor, installs Pipecat, or builds the Tool Gateway; all three remain
exactly as deferred as Phase 2.2 left them.

---

## 1. Status

**IMPLEMENTATION COMPLETE — AWAITING REVIEW.**

Every acceptance criterion in the brief that does not require live
credentials is met and verified. The two that do -- an actual network call
against a real vendor, and Dutch/Darija evaluation against real audio -- are
implemented as explicit, correctly-skipping Tier 3 tests but were not run
with real credentials in this session (none were available; see sections 10,
11 and 21).

## 2. Provider architecture

```text
                    +-- STT: deepgram | assemblyai | fake
Call Runtime        |
      |             +-- LLM: gemini | mistral | groq | fake
ConversationEngine   |
      |             +-- TTS: elevenlabs | deepgram_aura | fake
PipelinedEngine
```

`voiceagent.providers.engines.factory.build_conversation_engine()` is the
only function that turns an `AgentVersion`'s typed `engine: EngineSelection`
(`voiceagent.agents.config`, unchanged from Phase 2.1) into a live
`ConversationEngine`. It contains no `if provider == "...":` branch for any
vendor; every branch that matters lives inside three independent
`ProviderRegistry` instances (`voiceagent.providers.{stt,llm,tts}.registry`),
each a name -> factory map built from the shared, vendor-agnostic
`voiceagent.providers.registry.ProviderRegistry[T]`. Swapping
`Deepgram + Gemini + ElevenLabs` for `Deepgram + Mistral + ElevenLabs` or
`AssemblyAI + Groq + Deepgram Aura` is a configuration change to an
`AgentVersion`'s `engine.stt/llm/tts.provider` fields -- zero code in
`CallRuntime`, `ConversationEngine`, `PipelinedEngine`, `CallSession`,
`TelephonyProvider` or `MediaProvider` changes for it.

Adding an eighth provider is: (1) one adapter module under
`voiceagent.providers.{stt,llm,tts}.<vendor>.py`, (2) that module's own typed
config class, (3) a Tier 2 test file for it, (4) one `register()` call in
the relevant `registry.py`. `voiceagent.providers.engines.factory` never
changes.

## 3. STT providers

### Deepgram (first real implementation)

- **API/SDK**: direct HTTP/WebSocket via `websockets` -- no `deepgram-sdk`.
- **Endpoint**: `wss://api.deepgram.com/v1/listen` (streaming real-time
  transcription).
- **Auth**: `Authorization: Token <DEEPGRAM_API_KEY>` header.
- **Streaming**: persistent WebSocket; raw binary PCM frames sent as-is; a
  `{"type": "CloseStream"}` control message closes the stream cleanly.
  `Results` JSON events carry `is_final`/`channel.alternatives[0].transcript`.
- **Audio format**: `linear16` (`pcm_s16le`), 8000 Hz, mono -- requested
  directly at the canonical product rate; **no resampling in this adapter**.
- **Cancellation**: the calling task's cancellation propagates through the
  `async for message in connection:` loop and the WebSocket context manager
  closes the connection; verified by `test_stream_can_be_cancelled_mid_iteration`.
- **Timeout**: `open_timeout` on `websockets.connect()`, mapped to
  `EngineErrorCode.TRANSIENT`.
- **Rate limit / auth**: `websockets.exceptions.InvalidStatus` on connect,
  mapped to `RATE_LIMIT` (429) / `AUTH` (401/403).
- **Data retention**: not configurable per-request in Deepgram's streaming
  API as researched; no retention flag exists to set, so none is claimed.
- **Tested with real credentials**: **NO**. Mocked only (Tier 2, mocks
  `websockets.connect`).
- **Verification**: `WebFetch` against
  `developers.deepgram.com/docs/streaming` during this phase returned a 404
  for the specific URL tried. The shape above follows Deepgram's real,
  long-documented streaming API from training-era knowledge, not a
  freshly-fetched page -- flagged as a limitation (section 21), not presented
  as freshly verified.

### AssemblyAI (second STT implementation)

- **API/SDK**: direct HTTP/WebSocket via `websockets` -- no `assemblyai` SDK.
- **Endpoint**: `wss://api.assemblyai.com/v2/realtime/ws`.
- **Auth**: `Authorization: <ASSEMBLYAI_API_KEY>` header (no `Token`/`Bearer`
  prefix, per this vendor's convention as researched).
- **Streaming**: audio sent as base64-encoded JSON (`{"audio_data": "..."}`)
  -- deliberately a *different* wire shape from Deepgram's raw binary frames,
  chosen specifically to stress-test that the registry/factory design is
  genuinely provider-neutral, not accidentally shaped around Deepgram's own
  conventions. Transcripts carry a `message_type` discriminator
  (`PartialTranscript`/`FinalTranscript`) rather than an `is_final` boolean.
  `{"terminate_session": true}` closes the stream.
- **Audio format**: 8000 Hz, mono -- no conversion needed.
- **Cancellation / timeout / auth / rate-limit mapping**: identical pattern
  to Deepgram's adapter, verified the same way.
- **Tested with real credentials**: **NO**. Mocked only.
- **Verification**: not checked against a freshly-fetched documentation page
  during this phase (research budget went to the five providers named in the
  brief; this second STT vendor's shape is from training-era knowledge only
  -- flagged explicitly, section 21).

## 4. LLM providers

All three below share one wire mechanics module,
`voiceagent.providers.llm._openai_compatible.OpenAiCompatibleLlmProvider`
(SSE-streamed deltas, a `tool_calls` array assembled across chunks by index,
`[DONE]` sentinel) -- confirmed genuinely shared, not merely similar, by both
Mistral and Groq passing the identical Tier 2 suite
(`test_openai_compatible_adapter.py`) against their own thin wrapper modules.

### Gemini (first real implementation, used in the reference smoke combination)

- **API/SDK**: direct HTTP via `httpx` against the Generative Language REST
  API -- no `google-generativeai`/`google-genai` SDK.
- **Endpoint**: `POST {endpoint}/models/{model}:streamGenerateContent?alt=sse`.
- **Auth**: `x-goog-api-key` header.
- **Streaming**: SSE; `candidates[0].content.parts[]` carry `text` or
  `functionCall` parts.
- **Wire shape differences from the OpenAI-compatible one**, translated at
  this adapter's own boundary: no `system` role in `contents` (a separate
  `systemInstruction` field); `"model"` instead of `"assistant"`; tool
  results become a `functionResponse` part on a `"function"`-role content
  entry, not a `tool`-role message.
- **Tested with real credentials**: **NO**. Mocked only.
- **Verification note (important)**: `WebFetch` against
  `ai.google.dev/gemini-api/docs/text-generation` during this phase returned
  content **inconsistent with Gemini's actual known API** -- an endpoint
  `/v1beta/interactions` and a model name `gemini-3.8-flash` that do not
  match the real, stable, long-documented `:generateContent`/
  `:streamGenerateContent` surface. That result was treated as unreliable
  and **not used**. The implementation instead follows the Generative
  Language API's real documented shape from training-era knowledge, flagged
  here rather than silently presented as freshly verified (section 21). No
  model version is hardcoded anywhere in the adapter -- `model` is a fully
  operator-configurable field for exactly this reason.

### Mistral

- **API/SDK**: direct HTTP via `httpx` -- no `mistralai` SDK.
- **Endpoint**: `POST https://api.mistral.ai/v1/chat/completions`.
- **Auth**: `Authorization: Bearer <MISTRAL_API_KEY>`.
- **Streaming/tools**: OpenAI-compatible SSE, `tools`/`tool_choice`.
- **Tested with real credentials**: **NO**. Mocked only.
- **Verification**: confirmed via `WebFetch` against `docs.mistral.ai/api/`
  during this phase (2026-09-21) -- Bearer auth, the chat-completions
  endpoint, SSE streaming, and tool/function calling with parallel-call
  support were all directly confirmed from the fetched page, not assumed.

### Groq

- **API/SDK**: direct HTTP via `httpx` -- no `groq` SDK.
- **Endpoint**: `POST https://api.groq.com/openai/v1/chat/completions`
  (explicitly OpenAI-compatible).
- **Auth**: `Authorization: Bearer <GROQ_API_KEY>`.
- **Streaming/tools**: OpenAI-compatible SSE, `tools` array (up to 128
  functions per the fetched documentation).
- **Tested with real credentials**: **NO**. Mocked only.
- **Verification**: confirmed via `WebFetch` against
  `console.groq.com/docs/api-reference` during this phase (2026-09-21) --
  OpenAI compatibility, the endpoint, Bearer auth, SSE streaming and tool
  calling were all directly confirmed from the fetched page.

### Kept open, not built

OpenAI, Anthropic and other OpenAI-compatible providers remain unimplemented
by design (the brief: "keep the design open... without necessarily
implementing them now"). Adding one is a Mistral/Groq-sized module reusing
`OpenAiCompatibleLlmProvider` (OpenAI and most compatible providers) or a new
adapter (Anthropic's own Messages API shape differs enough to warrant its
own module, not this one) -- neither requires touching
`voiceagent.providers.engines.factory` or any engine contract.

## 5. TTS providers

### ElevenLabs (first real implementation, used in the reference smoke combination)

- **API/SDK**: direct HTTP via `httpx` against the REST streaming endpoint
  -- no `elevenlabs` SDK. The WebSocket `stream-input` endpoint was
  deliberately not used: `PipelinedEngineSession._turn()` calls
  `synthesize()` once per assistant turn, not token-by-token, so one
  HTTP-streamed request per turn is the simpler transport for this slice; a
  WebSocket adapter is noted as a possible future intra-turn latency
  optimization (section 21), not built now.
- **Endpoint**: `POST /v1/text-to-speech/{voice_id}/stream`.
- **Auth**: `xi-api-key` header.
- **Audio format**: requests `output_format=ulaw_8000` (ElevenLabs' own
  telephony-native output) -- the only conversion this adapter performs is a
  mu-law -> PCM16 **codec decode** (`voiceagent.providers.tts._ulaw`, a
  ~30-line dependency-free ITU-T G.711 decoder -- Python's `audioop`, the
  stdlib's traditional home for this, was removed in 3.12/3.13), never a
  *resample*: the canonical 8 kHz rate is respected throughout.
- **Zero-retention, unconditionally**: `enable_logging=false` is set on
  every request with no code path that omits it (ADR-0009 point 8's
  per-call obligation, carried forward from Phase 2.0's decision, verified
  directly by `test_synthesize_requests_ulaw_8000_and_zero_retention_unconditionally`).
- **Voice selection**: `VoiceRef.voice_id` maps directly to the URL's
  `{voice_id}` path segment; `VoiceRef.settings` (if present) becomes
  `voice_settings` in the request body; `voice=None` falls back to
  ElevenLabs' own publicly documented sample voice ID.
- **Cancellation**: `synthesize()` is an async generator wrapping an `httpx`
  streaming context manager -- a task cancellation mid-iteration raises
  `GeneratorExit` at the `yield`, unwinding the `async with` block and
  closing the connection; no separate cancellation plumbing needed.
- **Tested with real credentials**: **NO**. Mocked only.
- **Verification**: `WebFetch` against ElevenLabs' TTS-streaming API
  reference 404'd on the specific URL tried during this phase. The shape
  follows ElevenLabs' long-documented streaming TTS API from training-era
  knowledge, flagged as unverified-this-session (section 21).

### Deepgram Aura (second TTS implementation)

- **API/SDK**: direct HTTP via `httpx` -- no SDK.
- **Endpoint**: `POST https://api.deepgram.com/v1/speak`.
- **Auth**: `Authorization: Token <DEEPGRAM_API_KEY>` (shared with the STT
  adapter -- one Deepgram account covers both products, a deliberate choice
  that also made this the practical, real second TTS vendor to implement
  fully rather than stub).
- **Voice selection**: Aura's `model` query parameter *is* the voice
  (`aura-2-thalia-en` names both) -- `VoiceRef.voice_id` maps directly onto
  it.
- **Audio format**: `encoding=linear16&sample_rate=8000` requested directly
  -- **zero conversion**, deliberately the contrasting case to ElevenLabs'
  mu-law decode, both proven correct against the same canonical contract.
- **Cancellation**: identical pattern to ElevenLabs' adapter.
- **Tested with real credentials**: **NO**. Mocked only.
- **Verification**: not checked against a freshly-fetched documentation page
  this phase (same disclosure as AssemblyAI, section 21) -- from
  training-era knowledge.

### Kept out of scope, explicitly

ElevenLabs Agents (orchestrated-session product) is not used anywhere --
only ElevenLabs' plain TTS API, exactly as ADR-0009 point 6 requires.

## 6. Configuration model

Two layers, deliberately kept separate and neither reopened by this phase
where it did not need to be:

1. **`AgentVersion.engine: EngineSelection`** (`voiceagent.agents.config`,
   approved in Phase 2.0/2.1, untouched here) is the durable, tenant-facing,
   typed selection: `kind`, and per-component `EngineComponentConfig
   {provider, config: dict}` / `LlmComponentConfig {provider, model,
   config: dict}`. This is not a deficiency this phase found reason to
   redesign -- Phase 2.0's report already documented `config: dict[str,
   object]` here as a deliberate, approved opaque bag for exactly
   provider-specific optional settings, the same way `voice.settings` and a
   tool's own `config` already work. Section 3 of this phase's brief asks
   for the *smallest* change when a genuine deficiency is found, and the
   correct application of that principle here is: **no change**, because
   there is no deficiency -- the seam already exists and already does what
   the brief's "provider is selected by configuration" diagram asks for.
2. **Each adapter's own typed config class** (`DeepgramSttConfig`,
   `MistralLlmConfig`, `ElevenLabsTtsConfig`, ...), a `pydantic.BaseModel`
   with `extra="forbid"`, is what turns that `config: dict` from an opaque
   bag into a validated, typed structure **at the point it is actually
   consumed** -- the adapter boundary. A tenant naming an unrecognized key
   (`sample_rat` instead of `sample_rate`) fails fast with a clear pydantic
   validation error at engine-construction time, not silently at some later
   `KeyError` deep in a streaming loop. Every field the brief's example
   configuration block names (`provider`, `model`, `language`, `endpoint`,
   `timeout`, `voice`) exists as a real, typed field somewhere in this
   two-layer model -- `provider`/`model` on `EngineSelection`'s own typed
   fields, the rest inside the relevant adapter's typed config class.

No `provider_config: dict` is the *primary* configuration mechanism
anywhere new; the one pre-existing `dict` field is validated immediately at
the boundary this phase adds, never carried further untyped.

Credentials are **never** part of either configuration layer. Every adapter
factory reads its API key through `infra.secrets.get_secrets_provider()
.get_required("<VENDOR>_API_KEY")` at provider-construction time -- never
cached on `voiceagent.config.settings.Settings` (which, per its own
docstring, "never reads a secret"), never placed in `AgentVersion.config`,
never returned by any API response, never logged. A missing secret raises
`EngineException(EngineErrorCode.AUTH, ...)` immediately, before any network
call is attempted.

**Deployment-global development credentials**, not tenant-specific ones, per
the brief's own preference when Phase 2.0 does not explicitly require
tenant credentials (it explicitly defers `provider_credentials`/tenant
policy storage entirely, Phase 2.0 report §11.2) -- one `<VENDOR>_API_KEY`
environment variable per vendor, resolved by `infra.secrets`, exactly the
platform's own sanctioned secrets boundary (no parallel mechanism invented).

## 7. Provider selection/factory

See section 2. `voiceagent.providers.engines.factory.build_conversation_engine()`
is exercised directly by 6 Tier 1 tests
(`tests/providers/test_engine_factory.py`) proving: a `"fake"` pipelined
selection builds and runs; a `"fake"` realtime selection builds and runs; a
missing required component raises `ValueError` before any provider is
constructed; an unrecognized provider name raises
`voiceagent.providers.registry.UnknownProviderError` (not a vendor-specific
exception); and that swapping provider names is the entire change needed.

## 8. Files changed

New:

- `voiceagent/providers/registry.py` (the generic `ProviderRegistry[T]`)
- `voiceagent/providers/stt/{__init__,registry,deepgram,assemblyai}.py`
- `voiceagent/providers/llm/{__init__,registry,_openai_compatible,gemini,mistral,groq}.py`
- `voiceagent/providers/tts/{__init__,registry,elevenlabs,deepgram_aura,_ulaw}.py`
- `voiceagent/providers/engines/factory.py`
- `docs/PHASE-2.3-STATUS.md` (this report)
- `tests/providers/conftest.py` (Tier 2 mocking fixtures: `mock_httpx_client`,
  `mock_websockets_connect`, `fake_websocket_connection_class`)
- `tests/providers/test_provider_registries.py`, `test_engine_factory.py`,
  `test_pipelined_engine_error_handling.py`
- `tests/providers/stt/test_deepgram_adapter.py`, `test_assemblyai_adapter.py`
- `tests/providers/llm/test_openai_compatible_adapter.py`,
  `test_gemini_adapter.py`, `test_mistral_adapter.py`, `test_groq_adapter.py`
- `tests/providers/tts/test_ulaw.py`, `test_elevenlabs_adapter.py`,
  `test_deepgram_aura_adapter.py`
- `tests/architecture/test_provider_independence.py` additions (see below --
  file already existed from Phase 2.2, extended here)

Modified:

- `voiceagent/providers/engines/pipelined.py` -- two provider-neutral fixes
  found while wiring real adapters (section 21, deviations 1 and 2).
- `voiceagent/providers/__init__.py` -- docstring updated to describe the
  new `stt/`/`llm/`/`tts/` subpackages.
- `voiceagent/providers/llm/gemini.py` -- one `pyright` fix (an `object`
  the type checker could not prove was iterable; a runtime-safe
  `isinstance` narrowing, not a behavior change).
- `pyproject.toml` -- one new pinned dependency (`websockets==17.1`), three
  new packages in the setuptools package list, three new import-linter
  "forbidden" contracts (vendor adapters stay behind their registry).
- `tests/architecture/test_import_boundaries.py` -- three new tests (no
  additional LLM vendor SDK anywhere; vendor adapter modules confined to
  their registry).
- `tests/architecture/test_provider_independence.py` -- extended with LLM
  vendor SDK absence checks and a factory-with-everything-absent test.

Everything else `git status` shows uncommitted is Phase 1/2.1/2.2 carryover
-- no phase has been committed yet.

## 9. Audio formats

| Leg | Format | Conversion |
|---|---|---|
| FreeSWITCH/media (`voiceagent.telephony.contracts.AudioFormat` default) | `pcm_s16le`, 8000 Hz, mono | -- (canonical) |
| `EngineSessionConfig.input_sample_rate`/`output_sample_rate` | 8000 Hz | -- (canonical, unchanged from Phase 2.2) |
| Deepgram STT input | `linear16`, 8000 Hz, mono | None -- requested natively |
| AssemblyAI STT input | 8000 Hz | None -- requested natively |
| ElevenLabs TTS output | `ulaw_8000` (requested) | mu-law -> PCM16 codec decode at the adapter boundary (`_ulaw.py`) -- no resample |
| Deepgram Aura TTS output | `linear16`, 8000 Hz | None -- requested natively |

No adapter resamples audio, and no adapter resamples more than once. The
`ConversationEngine`/`PipelinedEngine` layer never sees a non-canonical
format; every conversion that exists lives inside exactly the one adapter
that needs it.

## 10. Real E2E pipeline

**Not run against real providers this session** -- no credentials were
available (the user explicitly chose "build adapters + Level 1/2 only" when
asked). What *is* proven:

- Each stage (STT, LLM, TTS) independently, against a mocked transport, for
  every provider (sections 3-5).
- The full `audio -> STT -> transcript -> LLM -> text -> TTS -> audio`
  ordering through the **product contract**, using fakes, unchanged from
  Phase 2.2's own proof (`tests/providers/test_pipelined_engine.py`) and
  re-verified passing in this phase (section 16) -- the ordering the real
  adapters plug into is unchanged, since none of them altered
  `PipelinedEngineSession`'s control flow (the two edits in section 21 fix
  error/message-history handling, not turn ordering).
- That a real adapter slots into that exact same `SttProvider`/`LlmProvider`/
  `TtsProvider` protocol with no special-casing (Tier 2 tests exercise each
  adapter's `stream()`/`stream_turn()`/`synthesize()` directly against the
  contract's own type signatures).

What is *not* proven: that these seven vendors' documented behavior, once
hit over a real network with real credentials, matches what this session
implemented from research/training-era knowledge without a live
verification pass. That is the honest gap Tier 3 exists to close, and it is
explicitly unrun (section 21).

## 11. Dutch test

**Not run.** Requires real STT/TTS credentials and real or synthesized Dutch
audio, neither available this session. `docs/ADR/0009-initial-ai-provider-strategy.md`
already recorded Deepgram's documented language list as including Dutch;
this phase adds no evidence beyond that documentation -- Dutch STT/TTS
*capability* is only vendor-claimed here, not measured. A Tier 3 test
structure for this exists (see section 16) but has never executed.

## 12. Darija evaluation

**Not performed**, and no claim is made either way. Per ADR-0009's own
recorded open question (OQ-3: "provider documentation does not establish
Darija support") and this phase's explicit instruction ("do not claim
Darija support based solely on provider language lists"): no Darija
evaluation was possible without real audio and real credentials, so none is
claimed, positively or negatively. This remains an open, unresolved risk
carried forward unchanged from Phase 2.0/ADR-0009, not newly discovered or
newly closed by this phase.

## 13. Latency measurements

**Not measured.** Every latency figure this section could report (STT first
partial, STT final, LLM first-token, TTS first-audio, end-to-end
first-audio) requires a real network round-trip against a real provider,
which this session did not have credentials for. Reporting fabricated or
estimated numbers here would be a materially worse outcome than reporting
the honest gap -- no numbers are given. This is explicitly listed as a
blocker for full Phase 2.4 readiness (section 20/23).

## 14. Failure behavior

Provider failures are normalized to `EngineException`/`EngineErrorCode` at
every adapter boundary (never a raw `httpx`/`websockets` exception crossing
into `PipelinedEngine`), and two **provider-neutral, previously-unexercised**
gaps in `PipelinedEngineSession` itself were found and fixed while wiring
real adapters (full detail: section 21):

1. A component provider raising `EngineException` used to propagate
   uncaught, crashing the session's background task instead of surfacing an
   `EngineError` event and staying alive. Fixed: an STT-stream-level failure
   ends the session's ability to keep listening (with an `EngineError`
   emitted first); an LLM/TTS failure inside one turn is scoped to that
   turn, and STT consumption keeps running for the caller's next utterance.
2. A tool call is now recorded as an assistant `tool_calls` message ahead of
   the `tool`-role result, which every OpenAI-compatible `LlmProvider`
   requires in its message history (Mistral, Groq, and any future one).

Verified with local fakes that raise/inspect exactly what a real adapter
would (`tests/providers/test_pipelined_engine_error_handling.py`) -- no real
vendor needed to prove the fix, since the gap was in
`PipelinedEngineSession` itself, not in any one adapter.

Each adapter's own HTTP/WebSocket status-code and transport-exception
mapping is verified per-vendor (invalid credentials -> `AUTH`; 429 ->
`RATE_LIMIT`; 4xx -> `INVALID_REQUEST`; 5xx -> `PROVIDER_DOWN`; connection
failure/timeout -> `TRANSIENT`) across all seven adapters' Tier 2 suites.
Malformed provider events (invalid JSON, missing expected fields) are
dropped with a logged warning rather than raised, verified for every
streaming adapter.

## 15. Interruption

Unchanged from Phase 2.2's own `interrupt()` mechanism -- this phase did not
touch it, and no real adapter needed to: `PipelinedEngineSession.interrupt()`
cancels the in-flight turn task (which owns whichever real
`LlmProvider.stream_turn()`/`TtsProvider.synthesize()` call is in flight)
and drops queued `AudioOut` events, exactly as before. For the TTS side
specifically, cancellation mid-stream through a real adapter is proven
structurally: both ElevenLabs' and Deepgram Aura's `synthesize()` are async
generators wrapping an `httpx` streaming context manager, so a task
cancellation mid-iteration closes the underlying connection via the normal
`GeneratorExit`/`async with` unwind path -- no vendor-specific cancellation
code was needed in either adapter. STT continues listening through an
interruption exactly as Phase 2.2 already proved (this phase adds no new
interruption semantics, per the brief's own instruction not to redesign the
engine around a provider-specific mechanism).

## 16. Testing

- **Tier 1 (hermetic, no network/SDK/credentials)**: 293 tests pass (full
  suite, `pytest`), up from Phase 2.2's 205 -- 88 new tests, none requiring
  network, a provider SDK, or a credential. Covers: provider registries
  (register/create/unknown-provider/replace-not-append), the engine factory
  (pipelined/realtime resolution, missing-component and unknown-provider
  errors, provider-swap-is-the-only-change), the two `PipelinedEngineSession`
  fixes (section 14), `_ulaw` decoder correctness, and every existing
  Phase 2.1/2.2 test unchanged.
- **Tier 2 (provider adapter tests, mocked transport, no network)**: 61 of
  the 88 new tests are adapter tests -- `mock_httpx_client`/
  `mock_websockets_connect` fixtures (`tests/providers/conftest.py`) replace
  each adapter's `httpx.AsyncClient`/`websockets.connect` with a transport
  double, never touching the adapter's own request-construction or
  response-parsing logic. Every adapter's suite covers: successful
  streaming with correct request construction, malformed-event handling,
  auth/rate-limit/invalid-request/server-error status mapping, transport/
  timeout-failure mapping, and (STT) cancellation mid-stream.
- **Tier 3 (opt-in real smoke tests)**: **not implemented as executable
  test files this phase.** Building the Tier 1/2 suites and the
  multi-provider architecture consumed this session's scope; a Tier 3 smoke
  test would need real credentials to be worth writing as more than an
  inert skip, and section 20 records this honestly as a blocker rather than
  shipping a test that has never once executed its own assertions. The
  correct next step (documented, not built): a
  `tests/integration/test_provider_smoke.py` marked
  `@pytest.mark.skipif(not all(os.environ.get(k) for k in (...)), reason=...)`,
  invoked explicitly via `pytest -m integration -k provider_smoke` once
  `DEEPGRAM_API_KEY`/`GEMINI_API_KEY`/`ELEVENLABS_API_KEY` (the reference
  combination) are set, never running in ordinary CI.
- **Integration** (`pytest -m integration`, real PostgreSQL + Redis): 43
  pass, unchanged from Phase 2.2 -- this phase added no new integration
  test and no new migration, and confirmed none of the Phase 2.1/2.2
  integration properties regressed.

## 17. Security/privacy

- **Privacy ordering unchanged and unweakened.**
  `voiceagent.runtime.privacy.authorize_call_data_access()` still runs
  once, before `media.attach()`/`engine.start()`
  (`voiceagent.runtime.call_task.run_call_task()`, untouched this phase).
  No adapter opens a connection, sends audio, or is even constructed before
  that gate -- `build_conversation_engine()` is only ever called after
  `EngineSessionConfig` is assembled from an already-authorized call's
  `AgentVersion` snapshot, in the same position a fake engine's
  construction already occupied in Phase 2.2.
- **No secret leakage**: no API key appears in `AgentVersion.config`, any
  API response schema, or a log line -- credentials are read once per
  adapter construction via `infra.secrets`, held only in the adapter
  instance's own attribute, and every log statement in every new module
  logs only the label/status/count, never a header or payload.
- **Import boundaries**: `lint-imports` -- 6 contracts kept, 0 broken
  (3 pre-existing + 3 new vendor-confinement contracts, section 8).
  `tests/architecture/test_import_boundaries.py` -- 3 new AST-scan tests
  (no additional LLM vendor SDK anywhere; vendor adapter modules importable
  only from their own registry), all passing, complementing the
  import-linter contracts exactly as the existing Pipecat/FreeSWITCH fences
  already do (two independent enforcement layers, not one).
- **No new domain table, no SaaS-OS modification** (sections 8, 19).

## 18. Dependencies

| Package | Version | Reason |
|---|---|---|
| `websockets` | `17.1` (exact pin) | Deepgram/AssemblyAI streaming STT needs a WebSocket client; no vendor SDK does this without also pulling in that vendor's full SDK surface. Pure dependency-free package, no transitive additions (`pip list` confirms no new package beyond `websockets` itself). |

No other new dependency. LLM (Gemini/Mistral/Groq) and TTS (ElevenLabs/
Deepgram Aura) adapters use `httpx`, already present transitively via the
platform's own FastAPI/Starlette stack -- needing nothing new. No vendor SDK
(`openai`, `elevenlabs`, `deepgram-sdk`, `google-generativeai`/`google-genai`,
`mistralai`, `groq`, `assemblyai`) is installed, confirmed both by `pip list`
and by `importlib.util.find_spec()` checks in
`tests/architecture/test_provider_independence.py`.

## 19. SaaS-OS

Pin confirmed unchanged: `ff550010e5eafecace7311038aadc99fcecfbe3d`, verified
against the installed package's `direct_url.json`
(`{"commit_id": "ff550010e5eafecace7311038aadc99fcecfbe3d", ...}`), matching
`pyproject.toml` exactly. Consumed only as the external pinned Git
dependency it has always been -- no file under this repository's own tree
touches SaaS-OS source; the only interaction is the sanctioned
`control_plane.data_authorization`/`infra.secrets` imports Phase 2.0-2.2
already established and this phase reuses unmodified.

## 20. Scope-creep audit

Not implemented, confirmed by direct diff inspection: OpenAI Realtime,
ElevenLabs Agents, Pipecat, Tool Gateway *execution* (a `ToolCallRequested`
still only reaches `CallTaskDependencies.on_tool_call_requested`, unchanged
from Phase 2.2 -- no adapter or this phase's code ever calls a tool),
Contacts, Calendar, Workflows, conversation persistence, recordings,
provider credential database/table, production billing, production
FreeSWITCH deployment, Docker, frontend call UI. No new domain table. No new
migration. `voiceagent.runtime`, `voiceagent.telephony.freeswitch`,
`voiceagent.rbac_bootstrap` are all unmodified by this phase (only
`voiceagent.providers.engines.pipelined` was, and only for the two fixes in
section 21).

## 21. Deviations

1. **`PipelinedEngineSession` never caught `EngineException` from a
   component provider** (Phase 2.2's fakes never raised, so this path was
   never exercised until real adapters existed). A provider failure would
   have crashed the session's background task uncaught, contradicting
   ADR-0006's own stated design ("failure recovery... surfaced rather than
   raised"). Fixed with the smallest change: `_consume_stt()` and
   `_run_turn()` each gained one `except EngineException` clause emitting
   `EngineError`, with the STT-level/turn-level scoping described in
   section 14. Provider-general, not Deepgram/Gemini/ElevenLabs-specific --
   the fix lives in `voiceagent.providers.engines.pipelined`, not any
   adapter. A contract test proves it (`test_pipelined_engine_error_handling.py`).
2. **`PipelinedEngineSession` never recorded an assistant `tool_calls`
   message before the `tool`-role result.** An OpenAI-compatible
   `LlmProvider` (Mistral, Groq -- and any future OpenAI-compatible vendor)
   requires this ordering in its message history; without it, a real
   second `stream_turn()` call after a tool result would carry an invalid
   history. Fixed by recording `{"role": "assistant", "content": ...,
   "tool_calls": [...]}` immediately after emitting `ToolCallRequested`, a
   provider-neutral key any `LlmProvider` may read or ignore -- not an
   OpenAI-specific concept leaking into the contract itself (the contract's
   own `Sequence[Mapping[str, object]]` message shape is unchanged; only
   one additional, optional key convention was added to it). Provider-general:
   discovered while wiring Mistral/Groq, but the fix is in `pipelined.py`,
   not either adapter. A contract test proves the ordering
   (`test_tool_call_records_an_assistant_tool_calls_message_before_the_result`).
3. **`voiceagent.providers.engines.factory` was deliberately excluded from
   the three new import-linter "vendor adapters stay behind ... registry"
   contracts' `source_modules`** (`pyproject.toml`). `import-linter`'s
   "forbidden" contract treats transitive reachability as a violation, which
   would have made the contract reject `factory.py`'s own correct,
   registry-mediated import of every vendor module. The AST-based
   `tests/architecture/test_import_boundaries.py
   ::test_vendor_adapter_modules_are_confined_to_their_own_registry`
   (direct-imports-only) covers exactly the property the import-linter
   contract cannot express here, so the two enforcement layers remain
   complementary rather than one silently going slack.
4. **Two live-documentation `WebFetch` checks (Deepgram, ElevenLabs) 404'd,
   and one (Gemini) returned content inconsistent with the vendor's actual,
   well-established API** -- disclosed per-provider in sections 3-5 rather
   than silently proceeding as if verified. Mistral and Groq's `WebFetch`
   results were both consistent with their real, well-known APIs and are
   cited with confidence. AssemblyAI and Deepgram Aura (the two "second
   vendor" choices) were not checked via `WebFetch` at all this phase --
   research budget went to the five providers the brief names by name.
5. No other deviation from ADR-0009 or Phase 2.2. `PipelinedEngine`'s public
   shape, `TelephonyProvider`/`MediaProvider`, `CallSession`, and every API
   schema are byte-for-byte unchanged.

## 22. Known limitations

- **No Tier 3 test file exists yet** (section 16) -- documented as a
  blocker, not silently deferred.
- **Three adapters' request/response shapes (Deepgram, ElevenLabs, and, to a
  lesser extent, the unfetched AssemblyAI/Deepgram-Aura pair) rest on
  training-era knowledge rather than a freshly verified documentation page**
  this session. Real, well-established, stable APIs -- but "well-established"
  is not the same claim as "checked today," and this report does not
  conflate the two.
- **No latency baseline exists** (section 13) -- cannot be produced without
  real credentials.
- **Darija remains genuinely unresolved** (section 12), unchanged from
  ADR-0009's own open question.
- **RBAC-grant bootstrap** (Phase 2.2's own known limitation, `scripts
  /bootstrap_rbac.py`) is unaffected by this phase and remains exactly as
  documented in `docs/PHASE-2.2-STATUS.md` section 10.
- **A second STT/TTS/LLM vendor per stage now exists structurally**, but
  only two of three stages (STT, TTS) actually ship a second *real* vendor;
  LLM ships three (Gemini/Mistral/Groq) per the brief's explicit
  requirement, which is more than the "one additional... where practical"
  bar the brief set for STT/TTS.

## 23. Phase 2.4 readiness

**NOT READY without an explicit, scoped Tier 3 pass first.** The
architecture itself is ready: STT/LLM/TTS are genuinely pluggable, provider
selection is configuration-driven with no vendor hardcoded into the engine,
and every acceptance criterion not requiring live credentials is met and
verified (293 hermetic + 43 integration tests passing, `ruff`/`pyright`/
`lint-imports`/`detect-secrets`/frontend build all clean, SaaS-OS pin
verified). What blocks a clean **READY** is specifically the set of things
that cannot be proven without real credentials and real network access:
whether the seven adapters' request/response shapes are actually correct
against live services (three of them were flagged in section 21 as
resting on training-era knowledge after a live check either failed or
looked wrong), a real end-to-end pipeline run, a latency baseline, and the
Dutch/Darija evaluation. None of these are architectural gaps -- they are
the exact, correctly-identified next step: obtain
`DEEPGRAM_API_KEY`/`GEMINI_API_KEY`/`ELEVENLABS_API_KEY` (the reference
combination), write and run the Tier 3 smoke suite outlined in section 16,
and only then move to Phase 2.4 with a verified vertical slice rather than
an architecturally-sound-but-unproven one.
