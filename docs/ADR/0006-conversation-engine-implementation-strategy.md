# ADR-0006: ConversationEngine implementation strategy — Pipecat bounded to PipelinedEngine

Status: Accepted (Phase 0.1)
Date: 2026-09-21
Resolves: Phase 0 report §19 OD-2

## Context

Phase 0 fixed the runtime's central abstraction and this ADR does not reopen it:

```
ConversationEngine
├── PipelinedEngine(STT, LLM, TTS)
└── RealtimeEngine(provider)
```

What Phase 0 left open is what implements those two engines. Three options were
carried forward: (A) Pipecat as the runtime dependency, (B) a minimal
product-owned realtime turn loop, (C) a hybrid in which the product owns the
`ConversationEngine` contract and Pipecat may be used internally behind an
implementation boundary.

The decision must be made against one objective above all others: **prevent the
future runtime rewrite that Phase 0 explicitly prohibits.** A rewrite becomes
inevitable in exactly two ways — adopting a framework whose own abstractions
become the de-facto architecture, or hand-building a turn loop that later cannot
express a provider class it was not designed for. Popularity is not evidence
either way.

### What the work actually consists of

Decomposing "realtime voice agent runtime" into the parts that matter here:

| Concern | Difficulty | Is it our differentiator? |
|---|---|---|
| VAD / silence handling | Hard to tune well (Silero-class models, thresholds, noise) | No |
| Turn detection / endpointing | Hard; semantic end-of-turn is an active research area | No |
| Barge-in / interruption plumbing | Moderate; must cancel synthesis mid-frame and reconcile context | No |
| Frame pipeline, backpressure, resampling | Moderate; unglamorous and easy to get subtly wrong | No |
| STT/LLM/TTS vendor adapters | Individually easy, collectively endless | No |
| Duplex realtime (S2S) session protocol | Moderate; vendor-specific, fast-moving | No |
| Tool-call mediation into the Tool Gateway | Moderate | **Yes** — ADR-0003 is the product |
| Tenant/agent-version resolution and isolation | Moderate | **Yes** — ADR-0004, §14 |
| Call lifecycle against FreeSWITCH | Moderate | **Yes** — ADR-0002 |
| Conversation record, audit, billing, privacy | Moderate | **Yes** |

The first six rows are commodity infrastructure with real depth. The last four
are the product. Pipecat covers the first six well and knows nothing about the
last four — which is the correct division, not a deficiency.

### Where the two engine types differ materially

For `PipelinedEngine`, the framework does the heavy lifting: VAD, endpointing,
interruption, frame scheduling, resampling, and a large set of streaming
STT/LLM/TTS adapters. Rebuilding that to a comparable standard is months of work
on a problem nobody will pay this product for.

For `RealtimeEngine`, the calculus inverts. A speech-to-speech provider
(ElevenLabs Agents-style, OpenAI Realtime-style) owns VAD, turn detection,
interruption and synthesis *inside its own session*. What remains on our side is
protocol adaptation, reconnection, and — critically — mapping the provider's
tool-call events onto the Tool Gateway with the product's own authorization,
validation, idempotency and audit. Routing that through a general frame pipeline
adds a hop and an impedance mismatch on the exact path where the product's
security model lives, and it is the path most likely to require patching a
framework internal when a vendor changes its event shape.

### Evidence from the reference product

Dograh took option (A) and ended up maintaining a **fork** of Pipecat in-tree,
with `PIPECAT_PROVENANCE.md`, `PIPECAT_REBASE_PLAN.md`,
`UPSTREAM_1_43_TO_1_45.md` and `UPSTREAM_COMPATIBILITY.md` to manage it. That is
the concrete failure mode this ADR must prevent — not "Pipecat is bad", but
"a framework adopted without a fence becomes a fork".

## Decision

**Option C — hybrid, narrowly bounded.** Specifically:

1. **The `ConversationEngine` / `EngineSession` contract is product-owned and
   Pipecat-free.** No Pipecat type, frame, processor or exception appears in the
   contract, in its event types, or in any signature the Call Runtime sees. The
   runtime is written once, against the contract, and never against a framework.
2. **Pipecat may be the internal implementation of `PipelinedEngine` only.**
3. **`RealtimeEngine` is product-owned**: a direct adapter over the vendor's
   duplex session, translating its events into the same `EngineSession` event
   stream. Pipecat is not used there.
4. **Pipecat is an optional dependency**, declared as an extra
   (`voiceagent[pipecat]`) and pinned to an exact version. The product must
   remain installable, testable and runnable without it.
5. **Import fence, mechanically enforced.** `pipecat` (and any
   `pipecat_*` package) is importable only from
   `voiceagent.providers.engines.pipecat/`. An import-linter `forbidden`
   contract covers every other module, and CI fails on violation — the same
   discipline SaaS-OS applies to `stripe` inside `core/billing`.
6. **No fork, ever.** Pipecat is consumed as a pinned upstream release. Its
   internals are never subclassed to change behavior, monkeypatched, or vendored.
   Only its public service/processor composition API is used. If a requirement
   cannot be met that way, the answer is to implement that engine ourselves
   (point 8), not to patch the dependency.
7. **A non-Pipecat path always exists and always runs in CI**: a deterministic
   `FakeEngine` (used by every test that is not specifically an engine
   integration test) plus the product-owned `RealtimeEngine`. This is the
   structural guarantee that Pipecat is removable: at no point does the product
   have only Pipecat-backed engines.
8. **Documented exit criteria.** Pipecat is dropped from `PipelinedEngine` — and
   that engine implemented in-house — if any of the following is observed, with
   evidence: (a) meeting a product requirement needs a fork or a monkeypatch;
   (b) a measured latency regression on the critical path is attributable to the
   framework and cannot be configured away; (c) an upstream release cadence
   makes a re-pin routinely break us; (d) tenant isolation or the Tool Gateway
   boundary cannot be expressed without leaking framework concepts upward.
9. **Engine ≠ media transport.** The media socket to FreeSWITCH belongs to the
   `MediaProvider` (ADR-0002, 2026-09-21 amendment), not to the engine. The
   engine consumes and produces PCM frames through the contract. If Pipecat's
   transport model expects to own the socket, the adapter bridges it with an
   in-memory transport rather than surrendering the boundary. Validating this
   cheaply is the first task of the Phase 2 spike.
10. **Nothing is installed or implemented by this decision.** Phase 0.1 remains
    documentation-only; the dependency extra is declared in Phase 1's
    `pyproject.toml` and first exercised in Phase 2.

## Evaluation against the required dimensions

| Dimension | How the decision handles it |
|---|---|
| Streaming STT / LLM / TTS | Pipecat's adapters inside `PipelinedEngine`; the contract exposes only partial/final transcript, text delta and audio frame events |
| Realtime S2S, ElevenLabs-Agents-style, OpenAI-Realtime-style | Product-owned `RealtimeEngine`; vendor session events mapped directly to contract events — no framework in the path |
| Barge-in / interruption | Defined **by the contract** (`interrupt()`, `SpeechStarted`); each engine maps its own mechanism onto it. Conformance test asserts synthesis stops mid-frame within a measured bound |
| Turn detection, VAD, silence | Pipecat's, inside `PipelinedEngine`; the vendor's, inside `RealtimeEngine`. Never the runtime's concern |
| Cancellation | First-class on `EngineSession`; every engine must make it idempotent and safe mid-turn |
| Backpressure | Pipecat's frame pipeline for the pipelined path; explicit bounded queues in the realtime adapter. The contract specifies drop/lag behavior so the runtime does not need to know which |
| Provider switching | A provider change is an adapter change; a *class* change (pipelined ↔ realtime) is an engine swap. Both are below the contract |
| Tool calls during a live conversation | Surfaced as one contract event (`ToolCallRequested`) regardless of engine; always executed through the Tool Gateway (ADR-0003), never by the engine |
| Latency | Phase 2 spike measures framework overhead on the end-of-speech → first-audio path against a direct baseline; exit criterion 8(b) makes this a gate, not an assumption |
| Failure recovery | Typed error taxonomy in the contract (`auth`/`rate_limit`/`transient`/`invalid_request`/`provider_down`); reconnection is the adapter's job, fallback policy is the runtime's |
| Observability | §17 metrics are emitted by the engine adapter against the contract's events — never scraped from framework internals, which would couple us to them |
| Testability | `FakeEngine` makes the whole runtime testable with no network; one conformance suite runs against every engine |
| Dependency risk | Optional extra, exact pin, import fence, no-fork rule, always a non-Pipecat path, documented exit criteria |
| Long-term maintainability | The rewrite Phase 0 prohibits is structurally prevented: the runtime depends on a contract this product owns, and no engine is load-bearing for the contract's existence |
| FreeSWITCH media streaming | Owned by `MediaProvider`, not the engine (point 9) |
| Multi-tenant isolation | Never inside the engine. The engine receives a resolved, immutable session config derived from the published AgentVersion; it has no tenant concept and needs none |
| Future provider adapters | Added as an adapter behind one of the two engine types; the runtime does not change |

## Alternatives rejected

- **(A) Pipecat as *the* runtime dependency, both engine types and the transport
  layer** — rejected: its abstractions become the architecture, the realtime and
  tool-call paths (the product's security-critical ones) end up expressed in
  framework terms, and the observed end state in a comparable product is a
  maintained fork.
- **(B) Fully in-house turn loop for both engines** — rejected for
  `PipelinedEngine`: VAD, endpointing and interruption tuning are months of
  non-differentiating work, and a naive implementation's failure mode is a
  product that feels subtly wrong in a way that is hard to attribute. Retained
  as the fallback that exit criteria 8 triggers, which is precisely why the
  contract must stay framework-free.
- **Pipecat for both engines behind the boundary** — rejected: it adds a hop and
  an abstraction mismatch on the realtime path for little gain, and puts a
  framework between a vendor's tool-call event and the Tool Gateway.

## Consequences

Positive: the pipelined path gets mature VAD/endpointing/interruption and a
large adapter catalogue immediately; the realtime path stays small, direct and
fully understood; the runtime is written against one contract; Pipecat's removal
is a bounded, pre-planned operation rather than a rewrite.

Negative and accepted: two implementation styles must be maintained; the
conformance suite must be genuinely strict or the two engines will drift in
observable behavior; an extra adaptation layer exists between Pipecat and the
contract, whose overhead Phase 2 must measure rather than assume.

## Testing implications

- **Engine conformance suite** — one suite, run against `FakeEngine`, the
  Pipecat-backed `PipelinedEngine` and each `RealtimeEngine`. Asserts: event
  ordering and completeness; barge-in cancels synthesis within a stated bound;
  `interrupt()` and `close()` are idempotent and safe mid-turn; tool-call
  request/result round-trip including a timed-out and an errored result;
  backpressure behavior under a slow consumer; the error taxonomy for each
  failure class; usage reporting in neutral units; reconnection.
- **Default test path is Pipecat-free.** Everything outside engine integration
  tests runs on `FakeEngine`, so CI stays hermetic and fast, and a Pipecat
  outage or API change can never take the whole suite down.
- **Latency benchmark** as a first-class test artifact from the Phase 2 spike,
  with recorded numbers, so exit criterion 8(b) can be evaluated against
  evidence rather than impression.
- **Import-fence test** in `tests/architecture/`, asserting the contract module
  and the runtime import no Pipecat symbol — the executable form of point 1.

## Related

`docs/PHASE-0-ARCHITECTURE.md` §11, §19 OD-2, §22; ADR-0002 (media boundary);
ADR-0003 (tool mediation).
