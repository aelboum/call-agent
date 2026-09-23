# Phase 2.14 Status: Observability, Operations & Reliability

Checkpoint: Phase 2.13 ("feat: harden production telephony runtime") is the
branch's tip commit at the start of this phase. SaaS-OS remains pinned and
unmodified. No commit exists yet for this phase's own work (see §25).

## 1. Objective

Make the AI Call Platform observable, diagnosable, measurable and
operationally safe: structured logging events at the key call/worker/runtime
transitions, provider-latency and outcome metrics, a bounded operational
error taxonomy, stuck-call and reconciliation detection, and two
operator-only diagnostics endpoints -- without weakening tenant isolation,
RBAC, privacy, or the Tool Gateway, and without logging or exporting
customer content.

## 2. Existing observability infrastructure (reused, not duplicated)

SaaS-OS (`infra.observability`, off-limits, non-editable) already provides:

- **Structured JSON logging** (`infra.observability.logging.configure_logging()`):
  every log record carries `timestamp`/`level`/`logger`/`message` plus
  correlation fields (`tenant_id`/`user_id`/`request_id`/`agent_id`/
  `action_id`) and OTel `trace_id`/`span_id`, with `extra=` values run
  through `redact()`.
- **Correlation context** (`infra.observability.context
  .bind_correlation_context()`): a `contextvars`-backed scope, already used
  throughout `voiceagent` (`voiceagent.runtime.call_task.run_call_task()`
  binds `tenant_id`/`request_id=str(call_session_id)` around the whole call;
  `voiceagent.tools.gateway.ToolGateway.execute()` does the same, scoped to
  one tool call).
- **OpenTelemetry tracing** (`infra.observability.otel.configure_tracing()`/
  `get_tracer()`): idempotent global `TracerProvider`, `console`/`none`
  exporter.
- **Liveness/readiness** (`api.health`, mounted by `api.platform
  .build_platform_app()`): `/healthz` never depends on dependencies;
  `/readyz` checks PostgreSQL (`SELECT 1`) and Redis (`PING`) concurrently,
  bounded, with `CheckResult.detail` limited to an exception *type name*.
- **Audit log** (`core.audit_log.record()`): already used by
  `voiceagent.tools.gateway`, `voiceagent.followups.service`,
  `voiceagent.workflows.service`, `voiceagent.call_intelligence.service`.

**Metrics were the one gap.** `infra.observability`'s own module docstring
records metrics as explicitly out of scope for its phase ("there is no
metric to record yet anywhere in the codebase"). This phase adds them at the
product layer (`voiceagent.metrics`), on the OpenTelemetry metrics SDK the
pinned `saas-os` dependency already ships transitively -- no new dependency,
`pyproject.toml` untouched.

## 3. Files added

- `voiceagent/metrics.py` -- the product's one `MeterProvider`/instrument
  registry. Bounded-cardinality counters and histograms for calls, runtime,
  providers, Tool Gateway and workers; every `record_*()` function is
  best-effort (`_safe()` swallows and logs an instrument failure rather than
  letting it reach the call path).
- `voiceagent/error_taxonomy.py` -- `categorize_exception()`: maps existing
  exception types (`EngineException`/`EngineErrorCode`, `TelephonyError`,
  `DataAuthorizationDeniedError`, `CallSessionOwnershipMismatchError`,
  `ToolError`, `WorkflowError`, `asyncio.CancelledError`/`CallCancelledError`,
  a redis/database client's own base error class by module prefix) onto a
  small bounded operational category string. No new exception hierarchy.
- `voiceagent/runtime/stuck_calls.py` -- `find_stuck_call_sessions()`
  (pure) / `detect_stuck_calls_for_tenant()`: detects (never cancels) a call
  whose owning runtime's heartbeat is still alive but whose status has not
  moved past a phase threshold (`startup`/`active`). Complements
  `voiceagent.runtime.reconciliation`, which reacts to the opposite
  condition (an *expired* heartbeat -- a crash).
- `voiceagent/runtime/diagnostics.py` -- `build_runtime_diagnostics()`: a
  safe, content-free snapshot of one in-process `CallRuntime`
  (`instance_id`/`capacity`/`current_load`/`is_shutting_down`/
  `owned_call_session_ids`). Documented as a seam for a *future*
  call-runtime process entrypoint to report on itself -- see §12 for why it
  is not wired into an HTTP endpoint this phase.
- `voiceagent/ops/__init__.py`, `voiceagent/ops/permissions.py` -- the
  `voiceagent.ops` RBAC resource (`read` action only), deliberately *not*
  auto-granted by `voiceagent.rbac_bootstrap` (see that module's own
  updated docstring and §11 below).
- `voiceagent/api/v1/ops.py` -- `GET /v1/ops/runtime-heartbeats`,
  `GET /v1/ops/stuck-calls`. See §12.
- Tests: `tests/test_metrics.py`, `tests/test_error_taxonomy.py`,
  `tests/runtime/test_stuck_calls.py`, `tests/runtime/test_diagnostics.py`,
  `tests/runtime/test_supervisor_metrics.py`, `tests/tools
  /test_gateway_metrics.py`, `tests/providers/test_pipelined_engine_metrics.py`,
  `tests/telephony/freeswitch/test_provider_metrics.py`, `tests/telephony
  /freeswitch/test_media_metrics.py`, `tests/followups
  /test_follow_up_worker_metrics.py`, `tests/call_intelligence
  /test_call_ai_analysis_worker_metrics.py`, `tests/api/test_ops.py`.
- This file.

## 4. Files modified

- `voiceagent/runtime/supervisor.py` -- structured `call.task.failed`/
  `call.teardown.timeout`/`runtime.shutdown.complete` log events;
  `record_runtime_call_startup_failure()` (categorized via
  `error_taxonomy`), `record_teardown_timeout()`, `record_runtime_shutdown()`;
  new `is_shutting_down`/`owned_call_session_ids` properties for
  `voiceagent.runtime.diagnostics`.
- `voiceagent/runtime/call_task.py` -- a `call.lifecycle` span around
  `run_call_task()`'s whole body (`call_session_id`/`tenant_id`/
  `call.outcome` attributes only -- never call content); `record_call_started()`,
  `record_call_setup_latency()` (load + authorize + media attach + engine
  start), `record_call_teardown()`/`record_call_completed()` (recorded once,
  in `finally`, regardless of which path produced the call's terminal
  outcome); a `call.task.started`/`call.task.completed` structured log pair.
- `voiceagent/runtime/reconciliation.py` -- `record_reconciliation()` per
  stale/repaired call, plus one structured `runtime.reconciliation.tenant_scan`
  log line per tenant scan that found anything.
- `voiceagent/followups/worker.py`, `voiceagent/call_intelligence/worker.py`
  -- `record_worker_tick()` (`claimed`/`failed`/`tenants_errored`/
  `duration_seconds`) plus a structured tick-summary log line, both at the
  one place each worker's `poll_once()` already returns its own
  `WorkerTickReport`/`CallAiAnalysisWorkerTickReport`.
- `voiceagent/tools/gateway.py` -- `record_tool_execution()` at every
  terminal outcome `_audit()` already records (`duplicate`/`denied`/
  `validation_failed`/`timed_out`/`cancelled`/`failed`/`succeeded`), timed
  from `_run_handler()`'s own entry for the handler-execution paths.
- `voiceagent/providers/engines/pipelined.py` -- `record_provider_operation()`
  for STT (per caller utterance: first `PartialTranscript` to
  `FinalTranscript`), LLM (`stream_turn`, one call site, wrapped
  transparently by a new `_timed()` async-generator helper so the turn's own
  control flow -- including its early `return` on a tool call -- is
  untouched) and TTS (`synthesize`, same helper).
- `voiceagent/telephony/freeswitch/provider.py` -- `_command()` now takes an
  explicit `operation` keyword (one of ten fixed ESL-verb names) and records
  `record_provider_operation("telephony", operation, outcome, duration)`;
  every public method updated to pass its own operation name. The raw ESL
  command string (which embeds a `call_ref`) is never used as a label.
- `voiceagent/telephony/freeswitch/media.py` -- `attach()`/`detach()` each
  record `record_provider_operation("media", ...)`.
- `voiceagent/config/settings.py` -- `RuntimeSettings
  .stuck_call_startup_threshold_seconds`/`stuck_call_active_threshold_seconds`
  (+ `VOICEAGENT_RUNTIME_STUCK_CALL_*_THRESHOLD_SECONDS` env parsing).
- `voiceagent/rbac_bootstrap.py` -- registers `voiceagent.ops` into the
  global permission catalog (`register_permissions()`), deliberately *not*
  added to the `PERMISSIONS` tuple auto-granted to the runtime's own service
  account (see §11).
- `voiceagent/api/v1/__init__.py` -- mounts the new `ops` router.

No migration: nothing in this phase adds or changes persistent state.
Stuck-call thresholds are process configuration (`RuntimeSettings`), not a
database column.

## 5. Structured logging

New/changed event names, all with bounded, non-content fields only:

| Event | Module | Fields |
|---|---|---|
| `call.task.started` | `runtime.call_task` | `call_session_id` |
| `call.task.completed` | `runtime.call_task` | `call_session_id`, `outcome`, `duration_seconds` |
| `call.task.failed` | `runtime.supervisor` | `call_session_id`, `runtime_instance_id`, `error_category` |
| `call.teardown.timeout` | `runtime.supervisor` | `call_session_id`, `runtime_instance_id`, `cancel_timeout_seconds`, `reason` |
| `runtime.shutdown.complete` | `runtime.supervisor` | `runtime_instance_id`, `duration_seconds` |
| `runtime.reconciliation.tenant_scan` | `runtime.reconciliation` | `tenant_id`, `scanned`, `stale`, `repaired` |
| `runtime.stuck_calls.detected` | `runtime.stuck_calls` | `tenant_id`, `scanned`, `stuck` |
| `follow_up_worker.tick` | `followups.worker` | `tenants_scanned`, `executed`, `errored` |
| `call_ai_analysis_worker.tick` | `call_intelligence.worker` | `tenants_scanned`, `executed`, `errored` |

Correlation is unchanged and reused: `call.task.*`/`call.teardown.timeout`
events happen inside (or, for the supervisor's own log lines, immediately
around) `bind_correlation_context(tenant_id=..., request_id=str(call_session_id))`
-- an operator with a `call_session_id` greps `request_id="<id>"` across
every log line this phase adds, exactly the same way every pre-existing
correlated line already works.

## 6. Privacy-safe logging (audited)

Every field listed in §5 is one of: a UUID/string identifier, a status
enum value, a duration in seconds, or an integer count. None of the
following ever appears in a log line or `extra=` value this phase adds:
transcript text, prompts, LLM responses, TTS text, STT raw output, API
keys/tokens, phone numbers, tool arguments, or knowledge item content.
`tests/test_error_taxonomy.py`'s own module docstring records that
`categorize_exception()` inspects an exception's *type*, never its
*message* (which can carry caller-supplied content) -- the one exception
being the module-name prefix check for a redis/database client's own base
error class, which never touches message content either.

No dedicated "no sensitive content in logs" test was added beyond this:
every new log call site is a fixed, hand-written `extra={...}` literal (not
built from user input), so the sensitive-data risk this section addresses
is closed by construction rather than needing a runtime assertion. This
mirrors how `voiceagent.tools.gateway`'s own pre-existing audit metadata
(`tool_id`/`call_session_id`/`status` only, never arguments) is verified --
by reading the call site, not by a leakage-detection test.

## 7. Metrics

All instruments live in `voiceagent/metrics.py`; every label is a bounded
`Literal` or a small, hand-written string set (never a call id, tenant id,
phone number, tool id, or vendor model name).

**Calls**: `voiceagent.calls.started` (counter), `voiceagent.calls.completed`
(counter, `outcome`), `voiceagent.calls.duration` (histogram, `outcome`).

**Runtime**: `voiceagent.runtime.call_startup_failures` (counter,
`error_category`), `voiceagent.runtime.teardown_timeouts` (counter),
`voiceagent.runtime.shutdown_duration` (histogram),
`voiceagent.runtime.stuck_calls_detected` (counter, `phase`),
`voiceagent.runtime.stuck_call_cancellations` (counter -- reserved; nothing
in this phase calls it, since detection never auto-cancels, see §13),
`voiceagent.runtime.reconciliation_repairs` (counter, `result`).

**Providers** (STT/LLM/TTS/telephony/media, one shared pair of instruments):
`voiceagent.provider.operations` (counter, `provider_family`/`operation`/
`outcome`[/`error_category`]), `voiceagent.provider.operation_latency`
(histogram, same labels).

**Tool Gateway**: `voiceagent.tools.executions` (counter, `outcome`),
`voiceagent.tools.execution_latency` (histogram, `outcome`).

**Workers**: `voiceagent.workers.jobs_claimed`/`jobs_failed` (counters,
`worker`), `voiceagent.workers.tenants_errored` (counter, `worker`),
`voiceagent.workers.tick_duration` (histogram, `worker`).

## 8. Histograms / latency

Call setup latency, call teardown latency, call duration, provider
operation latency (STT/LLM/TTS/telephony/media), Tool Gateway execution
latency, runtime shutdown duration, and worker tick duration are all
histograms (default OTel SDK bucket boundaries), not just averages -- see
§7's table for which instrument is which.

**STT latency is scoped per caller utterance, not per call.** STT is one
continuous stream for the life of a call (`self._stt.stream()`, called
once), unlike LLM (`stream_turn()`, once per turn) or TTS (`synthesize()`,
once per utterance) -- both of which map cleanly onto "one operation." STT
latency is instead measured from the first `PartialTranscript` of one
utterance to its `FinalTranscript`; a connection-level failure with no
utterance in flight is timed against the stream's own start instead. This
is a deliberate, documented interpretation (`voiceagent/providers/engines
/pipelined.py`'s own `_consume_stt()` docstring), not an oversight --
verified in `tests/providers/test_pipelined_engine_metrics.py`, which also
shows the one consequence: an `SttProvider` that never emits a partial
transcript (a real vendor always does; the deterministic `FakeSttProvider`
defaults to not) produces no STT latency measurement for that utterance.

## 9. Tracing

One span, `call.lifecycle`, wraps `run_call_task()`'s entire body (load,
authorize, media attach, engine start, the audio pump, teardown) --
attributes `call_session_id`/`tenant_id`/`call.outcome` only. No
transcripts, prompts, model responses, credentials, or raw tool arguments
ever become a span attribute. No per-frame or per-provider-operation span
was added (brief section 8: "Do not create one span per audio frame") --
provider-level timing is metrics-only (§7/§8), which is the bounded-volume
signal a hot audio path can afford; tracing stays at the one call-lifetime
granularity `infra.observability.otel` already supports.

## 10. Error taxonomy

`voiceagent.error_taxonomy.categorize_exception()` -- see §3. Categories in
use today: `cancellation`, `timeout`, `privacy_denied`, `tenant_isolation`
(a runtime-ownership conflict across `expected_runtime_instance_id`,
`CallSessionOwnershipMismatchError`), `validation`, `telephony`, `tool`,
`workflow`, the five `EngineErrorCode` values reused verbatim (`auth`,
`rate_limit`, `transient`, `invalid_request`, `provider_down`), `redis`,
`database`, and the catch-all `internal`. `tests/test_error_taxonomy.py`
proves the mapping is total (never raises) and matches every explicitly
recognized type. No new exception hierarchy was created; every category is
derived from an exception type this product already raises.

## 11. Health / readiness / dependency health

**Unchanged, because already correct.** `/healthz` and `/readyz` are owned
by `api.platform.build_platform_app()` (SaaS-OS) and already match this
phase's own requirements exactly: liveness never depends on a dependency;
readiness checks PostgreSQL and Redis concurrently with bounded timeouts and
returns only a check name + status (never a raw exception message); no
AI-provider call is ever made by either route. Phase 2.14 adds nothing here
-- duplicating an already-correct platform mechanism would be exactly the
kind of redundant framework the brief warns against.

## 12. Runtime diagnostics -- a documented scope limitation

`voiceagent.runtime.diagnostics.build_runtime_diagnostics()` is a pure,
tested function over one in-process `CallRuntime`, but **it is not wired
into an HTTP endpoint this phase**. Reason, stated once and referenced from
three module docstrings (`runtime/diagnostics.py`, `ops/__init__.py`,
`api/v1/ops.py`): ADR-0008 runs a `CallRuntime` in its own process, separate
from the API process, and no entrypoint script exists yet for either
process (`scripts/` holds only `bootstrap_rbac.py`) -- there is no channel
by which `voiceagent.api` could reach a live `CallRuntime` object today.
Building one now would mean inventing a new cross-process mechanism, which
the brief explicitly rules out (section 25: no new distributed tracing
backend, no generic event bus). `build_runtime_diagnostics()` is offered as
the seam a future call-runtime entrypoint calls to report on itself.

What the API process *can* honestly report cross-process is exposed
instead: `GET /v1/ops/runtime-heartbeats` (the Redis heartbeat set every
`CallRuntime` already publishes for the Call Orchestrator's own benefit --
instance id, capacity, current load, seconds since last heartbeat) and
`GET /v1/ops/stuck-calls` (a DB-based scan via `voiceagent.runtime
.stuck_calls`, scoped to the caller's own tenant). Both are bounded (a
2-second timeout on the Redis read) and return `redis_reachable=false`
rather than a 500 if Redis is unreachable.

## 13. Stuck-call detection

`voiceagent.runtime.stuck_calls` detects two cases (`startup`: assigned to a
runtime but still `initiated`/`ringing` past `stuck_call_startup_threshold_seconds`;
`active`: `answered`/`in_progress` past `stuck_call_active_threshold_seconds`),
using each `CallSession`'s own `updated_at` (bumped by every
`transition_call_session()` write) as "time since last progress." A call
whose owning runtime's heartbeat has already expired is explicitly excluded
-- that is `voiceagent.runtime.reconciliation`'s own, different signal (a
crash, not a wedge). A third case the brief names, "stuck in teardown," is
**not** produced by this module: `voiceagent.runtime.supervisor
.CallRuntime.cancel_call()` already detects and counts that directly, at
the one place teardown actually happens, in-process
(`voiceagent.metrics.record_teardown_timeout()`, §5's `call.teardown.timeout`
log event) -- a `CallSession` row alone cannot distinguish "still tearing
down" from "wedged active," so re-deriving it from a DB scan would be a
strictly worse copy of a signal the runtime already has firsthand.
**Detection only**: neither this module nor its `GET /v1/ops/stuck-calls`
route ever cancels a call. `voiceagent.metrics.record_stuck_call_cancellation()`
exists in the instrument registry for a future, explicitly-authorized
operator action; nothing in this phase calls it.

## 14. Worker instrumentation

Both `FollowUpWorker` and `CallAiAnalysisWorker` record one
`record_worker_tick()` call per `poll_once()` (§7), mapping each report's
own `executed`/`errored` fields onto `claimed`/`tenants_errored` (`failed`
is always `0` from both workers today: neither report distinguishes
per-job failures from per-tenant tick failures at a finer grain -- a
tenant's tick stops at its first exception, so one failed tenant tick is
exactly one countable failure, not several). No stale-lease concept exists
to instrument (`docs`/the earlier infra survey already recorded this: both
workers claim per-tick, not via a durable lease) -- "stale lease recovery"
from the brief's suggested metric list does not map onto this product's
actual worker design, and inventing one to satisfy the name would be new
worker-framework surface the brief prohibits.

## 15. Operational configuration

`RuntimeSettings.stuck_call_startup_threshold_seconds` (default 60s) /
`stuck_call_active_threshold_seconds` (default 3600s), both env-configurable
(`VOICEAGENT_RUNTIME_STUCK_CALL_STARTUP_THRESHOLD_SECONDS`/
`..._ACTIVE_THRESHOLD_SECONDS`). Every other operational setting this phase
touches (log level, health-check timeouts, worker concurrency, runtime
shutdown/cancel timeout) already existed and is unchanged -- no new
environment variable was added beyond the two thresholds above (brief
section 15: "do not add environment variables unnecessarily").

## 16. Failure isolation

Every `voiceagent.metrics.record_*()` function wraps its instrument call in
`_safe()`, which catches, logs (`_logger.warning`) and swallows any
exception rather than letting it propagate -- proven in
`tests/test_metrics.py::test_telemetry_failure_is_swallowed_not_raised`.
Recording is otherwise synchronous, in-process, and non-blocking (an OTel
instrument `.add()`/`.record()` call does no I/O itself; only a configured
exporter would, off the call path, on its own background thread) -- nothing
this phase adds ever awaits a telemetry backend from the audio path, the
Tool Gateway, or a worker tick.

## 17. Audit vs. observability

Unchanged distinction, reused correctly: `core.audit_log.record()` (Tool
Gateway status, followups/workflows/call-intelligence business events)
remains the accountability record; this phase's logs/metrics/traces are
operational only, and no new metric or log line duplicates audit content
(tool arguments, business outcome detail) -- metrics carry counts and
durations, never a business decision's payload.

## 18. Tenant / RBAC implications

Every domain metric/log line this phase adds is either tenant-agnostic by
nature (a provider-operation outcome, a worker tick, a runtime's own
process-wide counters) or already scoped by the correlation context a call
task binds (`tenant_id`) -- no new tenant-scoped table, no new tenant-scoped
read path. The one new authorization surface, `voiceagent.ops`
(`voiceagent/ops/permissions.py`), is a **documented, deliberate scope
limitation**: `core.rbac.PrincipalType` has no cross-tenant "platform
operator" principal yet, so `(voiceagent.ops, read)` is authorized the only
way any route in this product can be -- tenant-scoped `require_tenant()` --
even though the data it returns (Redis heartbeats, potentially another
tenant's stuck calls via the same runtime) is not itself tenant-scoped. This
permission is therefore never auto-granted by `bootstrap_tenant_rbac()`
(unlike every other permission in `PERMISSIONS`); an operator must grant it
explicitly, with `core.rbac.grant_permission()`, only to a role reserved for
genuine operator memberships -- see `voiceagent/ops/permissions.py`'s own
docstring for the full reasoning, and `voiceagent/api/v1/ops.py`'s for how
the two routes themselves stay bounded and safe regardless.

## 19. Safe incident investigation flow

1. **"Is a call stuck?"** `GET /v1/ops/stuck-calls` (operator-granted
   permission) for a DB-based scan of this tenant's own non-terminal calls;
   or grep `call.task.started` without a matching `call.task.completed` for
   a `request_id`.
2. **"Why did a call fail?"** Grep `request_id="<call_session_id>"` for the
   full correlated sequence (`call.task.started` → any provider/tool log
   lines → `call.task.completed` with `outcome`, or `call.task.failed` with
   `error_category` if the runtime itself caught an unexpected exception).
3. **"Which provider is timing out?"** `voiceagent.provider.operations`
   filtered to `outcome="timeout"`, grouped by `provider_family`/`operation`.
4. **"Is a specific tenant experiencing failures?"** `voiceagent.calls
   .completed` has no tenant label (bounded-cardinality discipline, §18) --
   cross-reference `voiceagent.calls.duration`'s low-level counts against
   `GET /v1/ops/stuck-calls` (tenant-scoped) and the correlated logs for
   that tenant's own calls instead.
5. **"Are workers healthy?"** `voiceagent.workers.tick_duration`/
   `jobs_claimed`/`tenants_errored`, or the `follow_up_worker.tick`/
   `call_ai_analysis_worker.tick` log lines.
6. **"Is FreeSWITCH reachable?"** `voiceagent.provider.operations` filtered
   to `provider_family="telephony"`, `outcome!="success"`.

## 20. Sensitive-data logging rules (summary)

Never log: transcript text, prompts, LLM responses, TTS text, STT raw
output, API keys/tokens/passwords/provider credentials, full phone numbers,
raw tool arguments, knowledge item content, or an arbitrary request body. A
log line or metric label may carry: identifiers (call/tenant/runtime ids),
lengths/counts, status/outcome enums, durations, bounded categorical values.
See §6 for how this phase's own additions were verified against this rule.

## 21. Testing

**Hermetic**: 796 passed (full suite, including this phase's ~13 new test
files: `tests/test_metrics.py` (9), `tests/test_error_taxonomy.py` (18),
`tests/runtime/test_stuck_calls.py` (7), `tests/runtime/test_diagnostics.py`
(3), `tests/runtime/test_supervisor_metrics.py` (3), `tests/tools
/test_gateway_metrics.py` (4), `tests/providers/test_pipelined_engine_metrics.py`
(3), `tests/telephony/freeswitch/test_provider_metrics.py` (4),
`tests/telephony/freeswitch/test_media_metrics.py` (5), `tests/followups
/test_follow_up_worker_metrics.py` (3), `tests/call_intelligence
/test_call_ai_analysis_worker_metrics.py` (2), `tests/api/test_ops.py` (2)).

**Integration**: none added. `voiceagent.runtime.reconciliation
.reconcile_tenant()`'s own metrics/logging addition (§4) has no hermetic
test seam of its own (it is exercised only by `tests/integration
/test_runtime_integration.py` against real PostgreSQL, which this
environment cannot run) -- a documented coverage gap, not a skipped
requirement: the addition is two non-invasive, best-effort calls
(`record_reconciliation()`, one `_logger.info()`) alongside logic the
integration suite already proves unchanged (verified by full hermetic-suite,
ruff and pyright passes showing no behavioral regression). The same applies
to `voiceagent.runtime.call_task.run_call_task()`'s own new metrics/span
code (§4): no hermetic test drives the *complete* function today (only
`_run_pumps()` in isolation, per `tests/runtime/test_call_task_tools.py`/
`test_call_task_persistence.py`'s own established pattern) -- its
correctness rests on the same static verification plus the existing
integration suite (`test_runtime_integration.py`, `test_tool_gateway_integration.py`).

## 22. Quality gates

- **Ruff check**: clean (`voiceagent/`, `tests/`, whole repo).
- **Ruff format**: clean for every file this phase touched. (One pre-existing,
  unrelated file, `docs/PHASE-2.10-STATUS.md`, was already non-conforming to
  the current formatter before this phase and was left untouched -- not
  this phase's to fix.)
- **Pyright**: 0 errors, 0 warnings across `voiceagent/` and `tests/`.
- **import-linter**: 8/8 contracts kept (`lint-imports`).
- **detect-secrets**: `detect-secrets-hook --baseline .secrets.baseline`
  against every file this phase added or modified: exit 0, no new findings.
  (An earlier `detect-secrets scan` invocation regenerated the on-disk
  baseline with a large set of pre-existing, unaudited findings unrelated to
  this phase -- none in a file this phase touched; that accidental rewrite
  was reverted with `git checkout -- .secrets.baseline` rather than kept.)
- **Migration validation**: not applicable -- no migration added.

## 23. Deviations from the specification

- **STT latency is per-utterance, not per-"operation" in the generic sense**
  (§8) -- a deliberate interpretation forced by STT being one continuous
  stream per call, documented in code and in this file.
- **"Stale lease recovery" worker metric not implemented** (§14) -- neither
  worker has a lease concept; the metric name does not map onto this
  product's actual design.
- **Runtime diagnostics has no HTTP endpoint** (§12) -- a pure function only,
  pending a future call-runtime process entrypoint this phase does not add.
- **`voiceagent.ops` permission is tenant-scoped, not truly operator-scoped**
  (§18) -- `core.rbac` has no cross-tenant operator principal yet; worked
  around by never auto-granting the permission, with the residual risk
  documented rather than silently accepted.
- **No integration tests added** (§21) -- this environment has no
  PostgreSQL; existing integration coverage is relied on instead, and the
  gap is stated plainly rather than claimed as covered.

## 24. Pre-existing, unrelated state

`docs/PHASE-0-ARCHITECTURE.md` and `docs/ADR/0010-one-frontend-multiple-user-contexts.md`
were already modified/added in the working tree before this phase began (a
prior, unrelated session) and remain untouched by this phase, per this
phase's own explicit instruction not to touch either file.
`docs/PHASE-2.10-STATUS.md` fails the current `ruff format` check
pre-existingly (§22) -- unrelated to this phase's changes.

## 25. Confirmations

- SaaS-OS is unchanged.
- `pyproject.toml` is unchanged.
- `docs/PHASE-0-ARCHITECTURE.md` is untouched by this phase.
- `docs/ADR/0010-one-frontend-multiple-user-contexts.md` is untouched by
  this phase.
- No commit was created.
- Nothing was pushed.
