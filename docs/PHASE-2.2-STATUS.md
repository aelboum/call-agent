# Phase 2.2 — Call Runtime and Conversation Execution Substrate: STATUS

**Status**: IMPLEMENTATION COMPLETE — AWAITING REVIEW (not committed)
**Updated**: 2026-09-21
**SaaS-OS pin**: `ff550010e5eafecace7311038aadc99fcecfbe3d` (unchanged, verified)

Phase 2.0 (`docs/PHASE-2.0-ARCHITECTURE.md`), ADR-0008 (call runtime
topology) and ADR-0009 (initial AI provider strategy) are authoritative for
the architecture; this document records what was actually built against
them, on top of the completed Phase 2.1 domain foundation
(`docs/PHASE-2.1-STATUS.md`).

---

## 1. Objective

Prove the first executable call-agent substrate — runtime process, product-
owned `ConversationEngine`, `TelephonyProvider`, `MediaProvider`, and their
wiring into the Phase 2.1 `CallSession` lifecycle — entirely against
deterministic fakes, with no commercial AI provider, no Pipecat, and no Tool
Gateway. Phase 2.2's job is the *substrate*, not a working phone call.

## 2. Runtime architecture

One `call-runtime` process, one `asyncio` event loop, many concurrent
`CallSession`s (ADR-0008 point 1), implemented as `voiceagent.runtime
.supervisor.CallRuntime`:

```text
CallRuntime (one process, one event loop)
├── call task (call A) ── independently cancellable asyncio.Task
├── call task (call B)
├── call task (call C)
├── heartbeat loop (Redis, refreshed on an interval)
└── DatabaseBoundary (bounded ThreadPoolExecutor)
```

`start_call()` creates one task per call, keyed by `call_session_id`, and is
idempotent — a redelivered assignment for a call already running there is a
no-op, never a second task. `cancel_call()` cancels one task and awaits its
teardown; `shutdown()` cancels every owned call and deregisters the
runtime's heartbeat. Horizontal scaling is more `CallRuntime` processes,
never more calls per process past its configured `capacity`
(`RuntimeSettings.max_concurrent_calls`) — no per-call process, no session
replication, no active-active ownership, no distributed call locks, exactly
as ADR-0008 specifies.

**Error isolation** (`_wrap_call_task()`): every call task's exception is
caught at exactly one point, recorded (`error_for()`), and logged. It never
propagates to another call's task or to the event loop. Only
`asyncio.CancelledError` passes through unmodified, so cancellation still
works.

## 3. ConversationEngine

`voiceagent.providers.engines.contracts` (already established in Phase 1,
extended for this phase) is the one product-owned, Pipecat-free contract
both engine types share:

```text
ConversationEngine
├── PipelinedEngine(stt, llm, tts)
└── RealtimeEngine(provider)
```

Events (`AudioOut`, `PartialTranscript`, `FinalTranscript`, `SpeechStarted`,
`SpeechEnded`, `ToolCallRequested`, `TurnEnded`, `UsageReported`,
`EngineError`) express product concepts only — no Pipecat frame/processor,
no provider SDK type, no FreeSWITCH object, no asyncio transport internal
appears in this module, and `tests/architecture/test_import_boundaries.py` /
`test_provider_independence.py` assert this mechanically.

One contract deviation found and fixed during implementation: see §19.1.

## 4. PipelinedEngine

`voiceagent.providers.engines.pipelined.PipelinedEngine` composes an
`SttProvider` + `LlmProvider` + `TtsProvider` into one `ConversationEngine`,
entirely framework-free:

```text
audio input -> STT -> transcript -> LLM -> assistant text -> TTS -> audio output
```

`PipelinedEngineSession` runs STT consumption and the LLM/TTS turn on
separate `asyncio.Task`s (`_stt_task`, `_turn_task`) so `interrupt()` can
cancel a turn mid-synthesis (barge-in) without tearing down the session's
ability to keep hearing the caller. Proven against
`voiceagent.providers.engines.component_fakes` (`FakeSttProvider`,
`FakeLlmProvider`, `FakeTtsProvider`, `tests/providers
/test_pipelined_engine.py`): partial/final transcript handling, assistant
text streaming, TTS audio chunks, end-of-turn, tool-call round-tripping,
cancellation, and interruption/barge-in (mid-synthesis `interrupt()` drops
queued `AudioOut` events already produced by the cancelled turn).

## 5. RealtimeEngine

`voiceagent.providers.engines.realtime.RealtimeEngine` is a separate
execution model, not a wrapper around `PipelinedEngine` — it has no STT/LLM/
TTS composition, no turn buffer, no internal message list. It adapts exactly
one `RealtimeProvider` (a single stateful duplex vendor session) into the
same `ConversationEngine`/`EngineSession` shape `PipelinedEngine` presents,
so the Call Runtime is written once against either. `RealtimeProvider`
names no vendor. `FakeRealtimeProvider`/`FakeRealtimeProviderSession`
(`tests/providers/test_realtime_engine.py`) are the only implementation
Phase 2.2 ships — a real OpenAI-Realtime-shaped adapter is future,
out-of-scope work that would implement this same protocol in its own
`voiceagent.providers.engines.<vendor>` module.

## 6. TelephonyProvider

`voiceagent.telephony.contracts.TelephonyProvider` (established Phase 1,
unchanged) is the narrow product-owned interface: `originate`, `answer`,
`hangup`, `bridge`, `transfer`, `hold`/`unhold`, `send_dtmf`,
`start_recording`/`stop_recording`, `events()`. The only implementation is
`voiceagent.telephony.freeswitch.provider.FreeSwitchTelephonyProvider`,
built over an injected `EslConnection` (dependency injection — no module
opens its own ESL socket). It translates FreeSWITCH's ESL command syntax and
`Event-Name`/`Hangup-Cause` vocabulary into `voiceagent.telephony.contracts`
types at exactly this module's boundary; no other application module
imports FreeSWITCH-specific implementation details
(`tests/architecture/test_import_boundaries.py`'s FreeSWITCH fence).
`voiceagent.telephony.freeswitch.fakes.FakeEslConnection` drives
`tests/telephony/freeswitch/test_provider.py` deterministically: inbound
call (`CHANNEL_PARK`), answer, hangup with cause normalization, an unmapped
event dropped (not raised), transfer/bridge/hold/DTMF command translation,
and `-ERR` ESL responses raising `TransportError`.

## 7. MediaProvider

`voiceagent.telephony.contracts.MediaProvider`/`MediaStream` (Phase 1) plus
the FreeSWITCH-specific implementation added this phase:
`voiceagent.telephony.freeswitch.media.FreeSwitchMediaProvider`, over an
injected `MediaSocket` (again dependency-injected — no socket opened by this
module). It is the only place `mod_audio_stream`'s asymmetric wire protocol
(binary L16 PCM inbound, a `{"type":"streamAudio",...}` JSON/base64 envelope
outbound) is constructed or parsed; the product-owned `MediaStream` contract
above it deals only in opaque PCM `bytes`. `attach()`/`detach()` are keyed
per `CallRef`; `health()` reports `StreamHealth(attached, frames_sent,
frames_received)`. The engine never touches this transport directly — the
call task's `_run_pumps()` moves bytes between `media_stream` and
`engine_session`, and neither `ConversationEngine` implementation imports
`voiceagent.telephony` at all. Proven in
`tests/telephony/freeswitch/test_media.py`: attach/detach lifecycle, format
negotiation against `supported_formats()`, an unsupported format raising
`UnsupportedFormatError`, double-attach and detach-without-attach raising
`TransportError`, and the wire-envelope round trip.

## 8. FreeSWITCH adapter

Deterministic only, no live server required by the default suite (brief
section 29): `FakeEslConnection`/`FakeMediaSocket`
(`voiceagent.telephony.freeswitch.fakes`) drive every hermetic test. The
actual ESL command strings and `Event-Name`/`Hangup-Cause` vocabulary
(`uuid_answer`, `uuid_kill`, `uuid_bridge`, `uuid_hold`, `uuid_send_dtmf`,
`uuid_record`, `originate`, the FreeSWITCH event names in `provider.py`'s
`_EVENT_TYPE_MAP`) are carried forward from Phase 0's own research pass and
are **not independently re-verified against a live FreeSWITCH instance in
this phase** — recorded here explicitly, matching Phase 0's own posture on
`mod_audio_stream`'s wire protocol before its conformance test existed. No
live ESL integration test exists yet; if one is added in a future phase, it
must be marked `integration`, documented, and kept out of the default CI
suite (brief section 29) — deferred, not attempted here. No production
FreeSWITCH deployment configuration was added.

## 9. Runtime ownership

A `CallSession` is assigned to a runtime exactly once, before media
attachment, using the two fields Phase 2.1's migration already carries on
`call_sessions` — `runtime_instance_id`, `runtime_assigned_at` — no new
`runtime_assignments` table.

- `voiceagent.calls.service.claim_runtime_ownership()` (Phase 2.1
  foundation, exercised for the first time this phase) is the atomic,
  idempotent claim: claiming a call already owned by the *same* runtime
  instance is a no-op (does not re-stamp `runtime_assigned_at`); claiming a
  call already owned by a *different* runtime raises
  `CallSessionAlreadyOwnedError` — ownership is exclusive by construction,
  never silently taken over.
- `voiceagent.runtime.assignment.select_runtime_for_assignment()` picks the
  least-loaded live runtime with `current_load < capacity`
  (`RuntimeHeartbeat.has_capacity`); `assign_call_to_runtime()` composes
  selection with the atomic claim and raises `NoRuntimeCapacityError` if
  every known runtime is at or above its ceiling — a defined outcome, never
  a silent wait or retry loop.
- Ownership is observable: `call_sessions.runtime_instance_id`/
  `runtime_assigned_at` are ordinary columns, already indexed
  (`ix_call_sessions_runtime_instance_id`, carried from Phase 2.1).

**Redis heartbeat** (ADR-0008 point 8):
`voiceagent.runtime.heartbeat.RuntimeHeartbeat` — `instance_id`, `address`,
`capacity`, `current_load`, `last_heartbeat_epoch_seconds` — written to
`voiceagent:runtime:{instance_id}` on a short TTL by `RedisHeartbeatStore`
(over `redis.asyncio`, client constructed lazily on first use — importing
the module opens no socket) and refreshed continuously by
`CallRuntime._run_heartbeat_loop()`. Staleness detection relies on Redis key
*expiry*, not comparing the stored timestamp to wall-clock "now" — a runtime
whose key has expired is simply absent from `read_all()`. Redis holds
liveness/coordination only; PostgreSQL (`call_sessions`) remains the
authoritative, durable store for which runtime owns a call — nothing in
Redis is ever treated as a call's source of truth.
`voiceagent.runtime.fakes.FakeHeartbeatStore` is the deterministic in-memory
double every hermetic test uses instead; a real Redis instance is exercised
only in `tests/integration/test_redis_heartbeat_integration.py`.

## 10. Reconciliation

`voiceagent.runtime.reconciliation.reconcile_tenant()` scans one tenant's
non-terminal `call_sessions`, finds every one whose `runtime_instance_id` is
set but absent from the current heartbeat set
(`find_stale_call_sessions()`), and transitions each to `interrupted` with
`end_reason="runtime_crashed"`. **It deliberately does not implement
takeover** — a stale call is marked ended, never reassigned to a different
runtime; ADR-0008 records session replication/hot-takeover as an explicit,
deferred Phase 3+ candidate (Phase 2.0 report §21 OQ-4), and this phase
holds that line. Idempotent: a call already reconciled is already terminal
and excluded from the next scan. A repair that races a legitimate,
independent hangup (`InvalidCallSessionTransitionError`) is treated as
already-resolved, not a reconciliation failure.

**Documented scope limitation**: `reconcile_tenant()` scans one tenant per
call, because no SaaS-OS primitive available at the pinned SHA enumerates
tenants across the Row-Level Security boundary. A background loop visiting
every tenant is therefore a loop over a tenant list from elsewhere (an
operator-maintained directory, or a future SaaS-OS primitive), calling
`reconcile_tenant()` once per tenant — not built in this phase (no
production-scheduling wiring is in scope), but the function itself is
proven per-tenant (`tests/runtime/test_reconciliation.py`,
`tests/integration/test_runtime_integration.py`).

## 11. Call lifecycle integration

`voiceagent.runtime.call_task.run_call_task()` runs one call from load
through teardown, driving the *existing* Phase 2.1
`voiceagent.calls.service.transition_call_session()` state machine — no
second, runtime-only lifecycle state machine was created:

```text
[orchestrator, before this module: tenant resolution, CallSession creation,
 Agent/AgentVersion selection, runtime ownership via
 voiceagent.runtime.assignment]
    |
load CallSession + AgentVersion snapshot   (DatabaseBoundary.run)
    |
privacy authorization                      (DatabaseBoundary.run; DENY -> stop)
    |
media attachment + ConversationEngine.start()
    |
initiated -> answered -> in_progress       (DatabaseBoundary.run, each)
    |
audio <-> engine event pump (_run_pumps) -- no database access anywhere here
    |
teardown: close engine, detach media, finalize CallSession (DatabaseBoundary.run)
```

The entire body is one `try`/`finally`: cancellation (hangup, runtime
shutdown, provider/media disconnect) may land at any point, including
before media is attached or the engine has started (exercised directly by
`test_run_call_task_happy_path_completes_and_finalizes` and
`test_ten_simultaneous_calls`), and whatever was created by that point is
always torn down, with the `CallSession` always finalized to a terminal
status. `_final_status()` maps a cancellation reason to the *ideal* terminal
status (`hangup` → `completed`, `runtime_shutdown` → `interrupted`,
`provider_disconnect`/`media_disconnect` → `failed`), falling back to
`interrupted` when that target is not legally reachable from wherever the
call actually got to (e.g. a hangup before the call was ever answered) — the
lifecycle transition table itself, unmodified from Phase 2.1, is what
enforces which statuses are reachable from where. A redelivered terminal
event and a delayed event are both handled by the existing lifecycle table's
own no-op/rejection semantics (Phase 2.1, re-verified unchanged in this
phase's hermetic suite).

## 12. Privacy authorization

Enforced by construction, not by convention: `run_call_task()` calls
`voiceagent.runtime.privacy.authorize_call_data_access()` — which wraps
SaaS-OS's own `control_plane.data_authorization.authorize_data_access()`
unchanged, no parallel policy system — immediately after loading the
`CallSession`/`AgentVersion` snapshot and strictly *before* the line that
calls `deps.media.attach()`/`deps.engine.start()`. A `DENY` raises
`DataAuthorizationDeniedError`, which the `except` clause right after the
authorization call catches, transitions the `CallSession` to
`failed`/`authorization_denied`, and `return`s — the media-attach and
engine-start lines are never reached on that code path; there is no branch
that reaches them without authorization having returned `ALLOW` first.
Evaluated exactly once per call, never per audio frame — no call site exists
inside `_run_pumps()` or anywhere in the per-frame path.

`voiceagent.runtime.privacy.AiDataPolicySource` is the seam; its only Phase
2.2 implementation, `StaticAiDataPolicySource`, is a deployment-wide default
built from `AiProviderSettings` (no per-tenant policy table exists yet —
Phase 2.0 report §11.2 defers that; documented as a known limitation, §20
below). `authorize_data_access()`'s `actor_user_id` requires a real
`core.users.id`; the call runtime has no human user in a call's context, so
`RuntimeSettings.system_actor_user_id` must be configured, and
`authorize_call_data_access()` fails closed (`PrivacyConfigurationError`)
rather than guessing a UUID.

Tested both directions: `tests/runtime/test_privacy.py` (allow/deny at the
function level, fail-closed on missing actor),
`tests/integration/test_runtime_integration.py::
test_run_call_task_denied_authorization_never_starts_the_engine` (a real
`_FailIfStartedEngine.start()` that raises `AssertionError` if ever called —
the ordering invariant proven against the actual call task, not just the
authorization function in isolation).

## 13. RBAC bootstrap

Resolves Phase 2.1's documented limitation (its status report §10: RBAC
permissions declared but never granted to any role). `voiceagent
.rbac_bootstrap`:

- `register_permissions()` idempotently declares every `voiceagent.*`
  permission (`agents.permissions.register()`,
  `calls.permissions.register()`, `phone_numbers.permissions.register()`)
  into SaaS-OS's global catalog. Never called at import time — covered by
  `tests/architecture/test_import_side_effects.py`.
- `PERMISSIONS` is one explicit tuple — `(agents, read)`, `(agents, write)`,
  `(calls, read)`, `(phone_numbers, read)`, `(phone_numbers, write)` —
  exactly what every domain module declares today, not a dynamic discovery
  mechanism and not a superuser grant.
- `bootstrap_tenant_rbac(tenant_id, actor_user_id, role_name=
  "voiceagent-runtime", service_account_id=None, scope=RoleScope.SELF)`
  creates or reuses that role, grants it every permission in `PERMISSIONS`,
  and — if a `service_account_id` is given — assigns the role to it (the
  call runtime's own acting principal). Every step tolerates "already
  exists" (`Duplicate*Error`) as success: safe to run repeatedly, no
  duplicate audit entry. Auditing itself is not reimplemented — every
  SaaS-OS primitive called here (`create_role`, `grant_permission`,
  `assign_service_account_role`) already writes its own `core.audit_log`
  entry.
- `scripts/bootstrap_rbac.py` is the operator-invoked entry point (`python
  -m scripts.bootstrap_rbac --tenant-id ... --actor-user-id ...
  [--service-account-id ...]`) — an explicit, one-time step, never a hidden
  startup mutation.

Tested in `tests/integration/test_rbac_bootstrap_integration.py`: full
bootstrap against a real database, idempotent re-run producing no duplicate
role/grant, an authorized API operation succeeding once the grant exists,
and an unauthorized operation (no bootstrap run, or a different tenant)
still denied.

## 14. Concurrency

All in `tests/integration/test_runtime_integration.py` (real PostgreSQL;
`voiceagent.runtime.fakes.FakeHeartbeatStore` stands in for Redis, since
this suite's job is the runtime's own logic, not re-proving `redis.asyncio`
itself):

| Test | Result |
|---|---|
| `test_two_simultaneous_calls` | 2 concurrent calls, both reach `completed` on hangup, `current_load` 2 → 0 |
| `test_ten_simultaneous_calls` | 10 concurrent calls, all reach `completed`, `current_load` 10 → 0 |
| `test_one_call_failing_does_not_affect_others` | a broken engine's `start()` raises; `error_for()` records it; the healthy call keeps running and completes normally |
| `test_cancelling_one_call_does_not_cancel_another` | cancelling call A leaves call B running, confirmed via `is_running()` before B is cancelled |
| `test_shutdown_cancels_all_owned_calls_and_deregisters_heartbeat` | heartbeat observed alive (`"rt-shutdown" in heartbeat_store`) while 2 calls execute; `shutdown()` drives both to `interrupted`/`runtime_shutdown` and removes the heartbeat key |

No calls-per-process capacity claim is made anywhere in this report or the
code — that requires real-provider and media benchmarking, explicitly out
of scope (ADR-0008 point 15, OQ-1).

## 15. Cancellation

Propagation path, proven end to end:

```text
CallRuntime.cancel_call()  -- sets CancellationSignal.reason, calls task.cancel()
    |
run_call_task()'s single try/finally -- catches the CancelledError implicitly
via awaiting the pumps; finally always runs
    |
engine_session.close() (idempotent) + deps.media.detach()
    |
PipelinedEngineSession.close(): cancels _turn_task if in flight, signals
_audio_in with None, awaits _stt_task
```

`_run_pumps()`'s own `finally` cancels both the caller-audio pump and the
engine-events pump and awaits them with `return_exceptions=True` — no
orphaned background task can continue processing a completed call.
`interrupt()` (barge-in) is a narrower cancellation: it cancels only the
in-flight LLM/TTS turn (`_turn_task`) and drops already-queued `AudioOut`
events, while leaving the STT-consumption task alone so the session keeps
hearing the caller — proven in `tests/providers/test_pipelined_engine.py`.
Runtime shutdown, provider disconnect, and media disconnect all resolve to
distinct terminal statuses via `_final_status()` (§11 above); every one of
these paths is exercised in `tests/integration/test_runtime_integration.py`
and `tests/providers/test_pipelined_engine.py`/`test_realtime_engine.py`.

## 16. Provider independence

- `test_pipecat_is_not_installed` / `test_no_commercial_ai_sdk_is_installed`
  (`tests/architecture/test_provider_independence.py`): `importlib.util
  .find_spec()` returns `None` for `pipecat`, `openai`, `elevenlabs`,
  `deepgram` — not "our code doesn't import it", but "this environment
  cannot import it at all".
- `test_fake_engine_starts_a_session_with_pipecat_absent` /
  `test_realtime_engine_starts_a_session_with_pipecat_absent`: both engine
  types start and run a session in that same Pipecat-absent environment.
- `tests/architecture/test_import_boundaries.py`'s static import-linter
  contracts (§18 below) independently confirm no provider SDK or Pipecat
  import exists in any product contract module, and that FreeSWITCH-specific
  code never crosses `voiceagent.telephony.freeswitch`'s boundary.
- Provider substitutability: `voiceagent.providers.engines.component_fakes`
  is one `SttProvider`/`LlmProvider`/`TtsProvider` implementation;
  `voiceagent.providers.engines.realtime.FakeRealtimeProvider` is a second,
  structurally unrelated implementation of the wider
  `ConversationEngine`/`EngineSession` protocol pair. Neither
  `PipelinedEngine` nor `CallRuntime`/`run_call_task()` was modified to
  accommodate the second — both satisfy the same `Protocol`s
  `voiceagent.providers.engines.contracts` already defines.

## 17. Security

- **Tenant isolation**: unchanged from Phase 2.1 — all 19 original RLS/
  composite-FK/trigger integration tests still pass (`tests/integration
  /test_domain_rls_integration.py`), plus this phase's own
  `test_runtime_integration.py` and `test_rbac_bootstrap_integration.py`
  exercise `claim_runtime_ownership()`/`reconcile_tenant()` through the same
  tenant-scoped service layer, never a raw query.
- **Import boundaries**: `lint-imports` — 3 contracts kept, 0 broken
  (SQLAlchemy/psycopg fence, FreeSWITCH fence, Pipecat fence). Complemented
  by `tests/architecture/test_runtime_db_boundary.py`'s AST-level checks:
  neither `voiceagent.runtime.call_task` nor `voiceagent.runtime.supervisor`
  imports `voiceagent.db`/`voiceagent.tenancy.tenant_scope` directly, and no
  name inside `_run_pumps()` (the actual audio-adjacent loop) may even be
  *referenced* if it is one of the sync database-touching names
  (`get_call_session`, `transition_call_session`,
  `authorize_call_data_access`, `tenant_scope`, etc.) — stronger than "not
  called".
- **DB-on-import**: `tests/architecture/test_import_side_effects.py`
  extended to cover `voiceagent.runtime.heartbeat`,
  `voiceagent.runtime.privacy`, `voiceagent.rbac_bootstrap` — importing each
  opens no socket, no thread pool, no database connection, registers no
  permission.
- **Sync DB off the event loop**: every database/SaaS-OS call from
  call-runtime code crosses `voiceagent.runtime.db.DatabaseBoundary.run()`,
  which schedules onto a bounded, explicitly-sized `ThreadPoolExecutor`
  (`RuntimeSettings.to_thread_pool_size`) — never the bare asyncio default
  executor. Only at call-start, at (a future) tool-call boundary, and at
  call-end; never inside `_run_pumps()`.

## 18. Verification

| Check | Result |
|---|---|
| `pytest` (hermetic, default) | **205 passed**, 43 deselected (`-m integration`) |
| `pytest -m integration` (real PostgreSQL + throwaway Redis) | **43 passed** |
| `ruff check` | Clean |
| `ruff format --check` | Clean (111 files) |
| `pyright` | **0 errors** (2 latent errors found and fixed during this phase's verification pass — see §19.2) |
| `lint-imports` | **3 contracts kept, 0 broken** |
| `detect-secrets` | 0 real findings (the same scan-run mutation to `.secrets.baseline` from placeholder connection strings/passwords in test fixtures and CI config as Phase 2.1 documented; reverted, not a real finding) |
| Frontend `tsc --noEmit` / `vite build` | Clean, unaffected by this phase's backend-only changes |
| Offline migration SQL (`alembic upgrade head --sql`) | Renders correctly, single head (`0002_domain_foundation`) |
| Migration head count | Unchanged from Phase 2.1 — **no new migration**, per this phase's brief §32 |
| `\dt app.*` against the live test database | Exactly the same four Phase 2.1 tables (`agents`, `agent_versions`, `phone_numbers`, `call_sessions`) |
| Pipecat installed | **NO** (`importlib.util.find_spec("pipecat") is None`, verified both by architecture test and `pip list`) |
| OpenAI SDK installed | **NO** |
| ElevenLabs SDK installed | **NO** |
| Deepgram SDK installed | **NO** |
| SaaS-OS pin resolved | `ff550010e5eafecace7311038aadc99fcecfbe3d` (`direct_url.json`, re-verified) |
| SaaS-OS working tree | Untouched — consumed only as an installed pinned dependency, never forked/vendored/modified |

## 19. Deviations from Phase 2.0/2.1

1. **`TtsProvider.synthesize()`'s `voice` parameter was `VoiceRef`
   (non-optional) in the Phase 1 contract, but `EngineSessionConfig.voice`
   is `VoiceRef | None` (an agent version need not configure a voice, per
   Phase 2.0), and the shipped `FakeTtsProvider` already accepted `VoiceRef
   | None`.** Calling `synthesize(text, self.config.voice)` from
   `PipelinedEngineSession._turn()` with an unconfigured voice was a real,
   reachable type mismatch, caught by `pyright` while wiring
   `PipelinedEngine` in this phase. Fixed by widening the protocol's
   parameter type to `VoiceRef | None` — matching the fake's own already-
   correct behavior, not narrowing what a real provider must accept. A
   provider adapter is free to apply its own default voice when `None` is
   passed; nothing above the protocol changes. This is a contract-
   correctness fix surfaced by real composition, not a reopened design
   decision.
2. **Two further latent `pyright` errors**, both in test/implementation code
   written during this phase, fixed before this report: a test helper typed
   its collected events as `list[object]` instead of `list[CallEvent]`
   (`tests/telephony/freeswitch/test_provider.py`), and
   `PipelinedEngineSession._messages` was annotated `list[Mapping[str,
   object]]` while built from `list[dict[str, object]]` — `list` is
   invariant, so the assignment did not type-check even though it was
   always safe at runtime; corrected to `list[dict[str, object]]`, which is
   both accurate and what every call site actually needs.
3. **No other deviation.** Runtime topology, ownership fields, heartbeat
   shape, reconciliation's no-takeover posture, the privacy-authorization
   ordering, and the `ConversationEngine`/`TelephonyProvider`/
   `MediaProvider` contract shapes all match Phase 2.0/ADR-0008/ADR-0009
   exactly as specified.

## 20. Known limitations

- **`StaticAiDataPolicySource` is a deployment-wide default, not real
  per-tenant AI data policy enforcement.** No `TenantAIDataPolicy` table
  exists (Phase 2.0 report §11.2 defers it, and this phase adds no domain
  table, per brief §32). Phase 2.2 proves the *ordering* invariant — no
  audio reaches an engine before authorization succeeds — against this
  default; a real per-tenant policy store is future work that plugs into
  the already-established `AiDataPolicySource` seam without touching
  `run_call_task()`.
- **`RuntimeSettings.system_actor_user_id` requires a manually provisioned
  `core.users` row.** There is no automated `ServiceAccount`-to-`core.users`
  bridge; an operator must provision one and configure its id, or every
  call fails closed with `PrivacyConfigurationError` before ever reaching
  authorization.
- **The FreeSWITCH ESL command/event vocabulary is not verified against a
  live server** (§8 above) — carried forward from Phase 0's own research
  pass, unverified in this phase too, consistent with the brief's explicit
  "no live instance required" posture (§29).
- **`reconcile_tenant()` has no cross-tenant scheduling wired up** (§10
  above) — the function is correct and tested per-tenant; a background loop
  that calls it once per tenant, on some interval, across a tenant
  directory, is not part of this phase's scope.
- **`EngineSessionConfig.tools` is always empty.** `AgentVersion.config
  .tools` (Phase 2.0 report §9.3) names tool keys a future Tool Gateway
  registry would resolve into `ToolSpec` — no such registry exists yet
  (Tool Gateway is explicitly out of scope, brief §4/§21).
  `CallTaskDependencies.on_tool_call_requested` is the clean internal
  event/command seam the brief's §21 asks for: a `ToolCallRequested` event
  reaches it and nothing else happens — no database query, no external API
  call, no credential access, no `control_plane.orchestration` import.
- **No RBAC-bootstrap scheduling/automation** — `scripts/bootstrap_rbac.py`
  is a manual, operator-invoked command, by design (brief §25 explicitly
  forbids a hidden startup mutation).

## 21. Exact tests

New or extended this phase (all under `tests/`, hermetic unless marked):

- `tests/providers/test_pipelined_engine.py` — pipeline ordering, streaming,
  cancellation, interruption/barge-in, tool round-trip.
- `tests/providers/test_realtime_engine.py` — `RealtimeEngine`/
  `FakeRealtimeProvider` conformance, independent of `PipelinedEngine`.
- `tests/telephony/freeswitch/test_provider.py` — command translation,
  event normalization, unmapped-event drop, `-ERR` → `TransportError`.
- `tests/telephony/freeswitch/test_media.py` — attach/detach, format
  negotiation, double-attach/detach-without-attach errors, wire envelope.
- `tests/runtime/test_assignment.py`, `test_db.py`, `test_heartbeat.py`,
  `test_privacy.py`, `test_reconciliation.py` — each module's own unit
  conformance, hermetic (`FakeHeartbeatStore`, in-memory fakes).
- `tests/architecture/test_provider_independence.py` — Pipecat/commercial-
  SDK absence, fake engines running without them.
- `tests/architecture/test_runtime_db_boundary.py` — AST-level sync-DB fence
  on `call_task`/`supervisor`.
- `tests/integration/test_runtime_integration.py` (real PostgreSQL) —
  ownership, reconciliation, privacy authorization, one full call lifecycle,
  supervisor concurrency (2, 10, one-failing, one-cancelling, shutdown).
- `tests/integration/test_rbac_bootstrap_integration.py` (real PostgreSQL) —
  bootstrap, idempotent re-run, authorized/unauthorized API operation.
- `tests/integration/test_redis_heartbeat_integration.py` (real Redis) —
  `RedisHeartbeatStore` write/read/expiry/remove against an actual server.

All Phase 2.1 tests (`tests/agents`, `tests/calls`, `tests/phone_numbers`,
`tests/integration/test_domain_rls_integration.py`, and the rest) remain
green, unmodified in behavior.

## 22. Phase 2.3 readiness

**READY.** The execution substrate — runtime process, ownership, heartbeat,
reconciliation, both engine shapes, the telephony/media boundary, the
privacy-authorization ordering, and the RBAC bootstrap Phase 2.1 was
missing — is in place and proven against fakes. Phase 2.3 (or wherever the
first real STT/LLM/TTS/realtime provider is introduced, per ADR-0009) can
implement `SttProvider`/`LlmProvider`/`TtsProvider`/`RealtimeProvider`
without changing `CallSession`, runtime ownership, `TelephonyProvider`,
`MediaProvider`, or any API contract — exactly the acceptance criterion this
phase's brief set. The known limitations in §20 (per-tenant AI data policy,
reconciliation scheduling, live-FreeSWITCH verification) are scoped,
documented, and none block starting that work.
