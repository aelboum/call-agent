# Phase 2.4 — Tool Gateway Foundation & Controlled Call Tools: STATUS

**Status**: IMPLEMENTATION COMPLETE — AWAITING REVIEW (implementation complete,
not committed)
**Updated**: 2026-09-21
**SaaS-OS pin**: `ff550010e5eafecace7311038aadc99fcecfbe3d` (unchanged, verified)

This document records what Phase 2.4 actually built: the product-owned Tool
Gateway (ADR-0003) as the single mediation point between the
`ConversationEngine` and executable application capabilities, and the four
initial call-control tools (`call.hangup`, `call.transfer`, `call.hold`,
`call.resume`).

---

## 1. Objective

Establish `Agent / ConversationEngine -> Tool Gateway -> authorization /
validation / idempotency / audit -> application service -> TelephonyProvider`
as the only path from the AI layer to an executable capability. The AI/
provider layer never directly executes a tool, touches the database, calls a
SaaS-OS application service, or reaches an external service. No real
AI-provider credentials are required or used anywhere in this phase.

## 2. Architecture

```text
ToolCallRequested (voiceagent.providers.engines.contracts, unchanged)
    |
    v
ToolGateway.execute()                    voiceagent/tools/gateway.py
    |
    +-- idempotency replay check         bounded, in-memory, per call
    +-- resolve ToolDefinition            voiceagent/tools/registry.py
    +-- verify AgentVersion allowlist     voiceagent/tools/allowlist.py
    +-- validate input                    ToolDefinition.input_model (pydantic)
    +-- resolve tenant's service account  voiceagent/tools/gateway.py::_resolve_service_account
    +-- verify authorization              core.rbac.can(), via DatabaseBoundary
    +-- timed execution                   ToolDefinition.handler -> TelephonyProvider
    +-- validate output                   ToolDefinition.output_model
    +-- audit outcome                     core.audit_log.record(), via DatabaseBoundary
    |
    v
ToolResult (voiceagent.providers.engines.contracts, unchanged)
```

No second `ToolRequest`/`ToolResult` type was introduced: the gateway
consumes and produces the exact `ToolCallRequested`/`ToolResult`/`ToolSpec`
contract Phase 1/2.2 already defined in
`voiceagent.providers.engines.contracts`. The gateway is provider-neutral —
it never learns which `LlmProvider` produced a request.

## 3. Tool definitions implemented

Four, exactly the brief's "deliberately small initial tool set", each a
`ToolDefinition` (`voiceagent/tools/definitions.py`) registered once, at
import time, into the process-wide `TOOL_REGISTRY`
(`voiceagent/tools/registry.py`):

| tool_id | risk | handler calls |
|---|---|---|
| `call.hangup` | high | `TelephonyProvider.hangup()` |
| `call.transfer` | high | `TelephonyProvider.transfer()` (destination validated as strict E.164 before the handler ever runs) |
| `call.hold` | low | `TelephonyProvider.hold()` |
| `call.resume` | low | `TelephonyProvider.unhold()` |

`TelephonyProvider` already had all four methods (Phase 2.2) — no contract
change was needed. Every handler catches `TelephonyError` and normalizes it
to `ToolExecutionError("telephony_error", retryable=False)`; none imports
FreeSWITCH/ESL/`mod_audio_stream` — each calls exactly the injected
`TelephonyProvider` instance for *this* call's own `CallRef`
(`voiceagent/tools/handlers.py`).

Input/output are typed Pydantic models (`StrictToolModel`, `extra="forbid"`),
not a hand-maintained JSON Schema — `ToolDefinition.input_schema`/
`output_schema` derive the JSON-Schema view from the same model via
`model_json_schema()`, so the schema the model is shown and the schema that
actually validates its arguments can never drift apart.

## 4. AgentVersion allowlist behavior

No new field, no new table: `AgentConfig.tools` (`list[ToolBinding]`,
approved in Phase 2.0/2.1) already is the allowlist —
`voiceagent/tools/allowlist.py::is_tool_allowed()` reads `ToolBinding.key`
directly from the immutable, published `AgentVersion.config`. A tool ID not
listed is not permitted; the check never consults `TOOL_REGISTRY` (two
independent gates, ADR-0003 point 6). `_engine_session_config()`
(`voiceagent/runtime/call_task.py`) now resolves each bound key that is also
a real, registered tool into a `ToolSpec` the model is shown — an unknown key
is silently omitted from what is advertised, never a security decision by
itself (real enforcement is entirely the gateway's own allowlist-plus-registry
check at execution time).

## 5. Authorization behavior

Uses `core.rbac.can()` directly — no second RBAC system. `voiceagent/tools/
permissions.py` declares one `(RESOURCE="voiceagent.tools", tool_id)`
permission per tool; `voiceagent/rbac_bootstrap.py`'s `PERMISSIONS` tuple now
includes all four, computed from `TOOL_REGISTRY.known_tool_ids()` rather than
hand-listed a second time.

**A real design bug was found and fixed during this phase's own integration
testing, not left in place**: the acting principal is the call runtime's own
`core.identity.ServiceAccount`, and a `ServiceAccount`'s `tenant_id` is fixed
permanently at creation (verified against the pinned SaaS-OS source). A
single, deployment-wide `RuntimeSettings.system_service_account_id` (this
phase's first draft) therefore cannot authorize calls across more than the
one tenant it was created in — `core.rbac.can()` correctly fails closed for
every other tenant, and worse, `core.audit_log.record()`'s own
`fk_audit_log_tenant_service_account` constraint correctly rejects an attempt
to attribute an audit entry to a service account that does not belong to the
entry's own tenant, so the original code crashed with an unhandled
`IntegrityError` instead of returning a clean denial. **Fixed** by replacing
the fixed id with `RuntimeSettings.system_service_account_name` (a name, not
a UUID) plus `ToolGateway._resolve_service_account()`, which looks up the
tenant-local, `ACTIVE` service account with that name via
`core.identity.list_service_accounts(tenant_id)` at authorization time. An
operator now provisions one service account per tenant, all sharing the
configured name (`scripts/bootstrap_rbac.py --service-account-id`), and the
gateway resolves the right row for the call's own tenant every time. A tenant
with no matching account fails closed as `unauthorized`, audited as
`ActorType.SYSTEM` (no id to attribute cross-tenant) — never a crash, never a
silent skip. `docs/ADR/0003-tool-gateway-mediated-agent-actions.md`'s "Phase
2.4 addendum" records this and two smaller clarifications.

Two independent gates, per ADR-0003 point 6: allowlist membership and RBAC
permission must both pass; neither alone is sufficient.

## 6. Idempotency behavior

Bounded, in-memory, per-call: `ToolGateway` keeps a `dict[call_session_id,
dict[tool_call_id, ToolResult]]`, keyed on the provider-neutral
`ToolCallRequested.call_id` — never a provider-specific SDK object. A
duplicate delivery of the same `call_id` is answered from the cache (audited
as `duplicate`) without re-running validation, authorization, or the handler
a second time. `ToolGateway.forget_call()` clears one call's entry;
`voiceagent.runtime.call_task.run_call_task()` calls it once, in its own
teardown `finally` block, so dedup state never outlives the one call task it
was collected for.

**Deliberate deviation from ADR-0003 point 2's `core.idempotency
.run_idempotent()`**, documented in the gateway's own module docstring and
the ADR-0003 addendum: that primitive's `business_fn` is synchronous and runs
inside its own DB session/transaction, but a tool handler's real work here is
an async `TelephonyProvider` call — there is no way to run that inside a
synchronous, DB-session-scoped callback without either blocking the event
loop on network I/O under an open transaction or a thread-hop the primitive
was never designed for. The crash/restart boundary is stated plainly: this
state is runtime-process memory, for the life of one call task; if the
process crashes mid-tool-call, the record is gone — but Phase 2.2 implements
no crash takeover either, so there is no call task left running to ever
redeliver a duplicate to. No new durable idempotency table was introduced.

## 7. Timeout/cancellation behavior

Every `ToolDefinition.timeout_seconds` (10s for all four Phase 2.4 tools)
bounds its handler via `asyncio.wait_for()`; a timeout is normalized to
`ToolResult(error_code="timeout", retryable=True)` and audited `timed_out`.
Cancellation of the owning call propagates all the way to an in-flight tool
dispatch: `voiceagent.runtime.call_task._run_pumps()` tracks each spawned
`_execute_and_submit_tool_call()` task in a set and cancels/joins it
alongside the caller-audio and engine-event pump tasks in its own `finally`
block — no orphaned background task can continue processing a completed
call. `ToolGateway`'s own `_run_handler()` catches `asyncio.CancelledError`,
records a `cancelled` audit entry, and re-raises (cancellation is never
swallowed). No synchronous DB/network work runs directly on the event loop —
every DB-crossing step (`_resolve_service_account`, `_check_authorization`,
`_audit`) goes through `DatabaseBoundary.run()`, the identical seam Phase 2.2
already established; the audio pump itself never references any of those
names (`tests/architecture/test_runtime_db_boundary.py`, unchanged and still
green).

## 8. Audit behavior

`core.audit_log.record()` — no parallel audit table. One entry per terminal
outcome: `denied`, `validation_failed`, `started`, `succeeded`, `failed`,
`timed_out`, `cancelled`, `duplicate`. `resource_type="tool_call"`,
`resource_id=str(call_session_id)`, `correlation_id=str(call_session_id)`,
`metadata={"tool_id", "status", "call_session_id"}` — **never tool
arguments** (a transfer destination included), addressing the brief's own
"be careful with transfer destinations" instruction directly rather than
leaving it to reviewer discretion. `docs/ADR/0003-...md`'s addendum records
that ADR-0003 point 7's "persisted `ToolCall` record" is satisfied by these
audit-log entries, not a new bespoke table, given this phase's own
no-new-table constraint.

## 9. Engine integration

`voiceagent.runtime.call_task.run_call_task()`'s `_dispatch_tool_call()`
closure calls `deps.tool_gateway.execute(...)`; `_run_pumps()`'s
`pump_engine_events()` spawns a tracked task per `ToolCallRequested` that
awaits the dispatch and then calls `engine_session.submit_tool_result()` —
the exact `ConversationEngine -> ToolCallRequested -> ToolGateway ->
ToolResult -> ConversationEngine` flow the brief specifies. The engine itself
never imports or executes a tool: `PipelinedEngineSession` (Phase 2.3,
unmodified) already records the assistant `tool_calls` history message
*before* the tool-role result message, and that ordering is unaffected by
this phase. `CallTaskDependencies.on_tool_call_requested` (Phase 2.2's
placeholder sink) is retired, replaced by `tool_gateway`/`telephony`/
`system_service_account_name` fields.

## 10. Security/import-boundary verification

- `tests/architecture/test_tool_gateway_isolation.py` (new): no
  `voiceagent.tools` module imports SQLAlchemy/psycopg, FreeSWITCH
  internals, `voiceagent.db`/`tenant_scope` directly, or any commercial AI
  provider SDK; and — the reverse direction — no
  `voiceagent.providers.engines.*` module imports `voiceagent.tools` at all.
- A matching new `pyproject.toml` import-linter contract, "The
  ConversationEngine never imports the Tool Gateway", plus `voiceagent.tools`
  added to the existing FreeSWITCH/Pipecat/STT/LLM/TTS-vendor-confinement
  contracts.
- `tests/architecture/test_runtime_db_boundary.py` (Phase 2.2, unmodified):
  still passes unchanged — the audio pump references no sync-DB-touching
  name even with the new dispatch wiring.
- Tenant isolation for tool execution: `tests/integration
  /test_tool_gateway_integration.py::test_a_tenant_with_no_bootstrap_is_denied_
  even_though_another_tenant_is_fully_set_up` proves per-tenant resolution
  and authorization are both scoped correctly — a second tenant sharing the
  exact same service-account *name* as a fully bootstrapped one cannot borrow
  its authorization.

## 11. Full test counts and results

- Hermetic (`pytest`, default): **364 passed**, 0 failed (up from Phase
  2.3's 293 — +71 for this phase: `tests/tools/` — registry (8),
  allowlist (4), handlers (11), gateway (17) — plus
  `tests/architecture/test_tool_gateway_isolation.py` (6) and
  `tests/runtime/test_call_task_tools.py` (4), plus incidental additions to
  `tests/config/test_settings.py` for the new `system_service_account_name`
  field).
- Integration (`pytest -m integration`, real PostgreSQL 16 + Redis): **48
  passed**, 0 failed (up from 43 — +5 in `tests/integration
  /test_tool_gateway_integration.py`: real authorized execution, real
  unbootstrapped denial, real per-tenant isolation, real
  not-provisioned-yet denial, and one full `run_call_task()` execution
  dispatching a real tool call end to end).

## 12. Lint/type/import/security results

- `ruff check .`: clean.
- `ruff format --check .`: clean (159 files).
- `pyright`: **0 errors** (10 latent errors surfaced and fixed during this
  phase's own new code — nullable `AuditLogEntry.entry_metadata` access in
  two integration tests, a generator-fixture return-type annotation, a
  `dict[str, object]`-typed JSON-Schema value narrowed before `in`, and the
  `ToolHandler` callable-parameter-contravariance issue across all four
  handler registrations, resolved with an explicit, documented `cast()` at
  the one call site that needs the narrower type).
- `lint-imports`: **7 contracts kept, 0 broken** (one new contract added
  this phase).
- `detect-secrets`: 0 real findings (a scan-run baseline mutation from
  pre-existing placeholder connection strings in `.env.example`, CI config,
  and test fixtures — none in a file this phase touched — was reverted, not
  a real finding).
- Frontend `tsc --noEmit` / `vite build`: clean, unaffected (no frontend
  work this phase).

## 13. SaaS-OS

Confirmed: `ff550010e5eafecace7311038aadc99fcecfbe3d`, matching the
installed package's `direct_url.json` and `pyproject.toml`. Consumed only as
an external pinned dependency — untouched, unforked, uncopied, unmodified.

## 14. Scope-creep audit

Not implemented, confirmed by inspection of the diff: realtime provider,
OpenAI Realtime, ElevenLabs Agents, Pipecat, Contacts, Calendar, external
CRM/calendar integrations, a generic/workflow automation engine, arbitrary
custom-code tools, conversation persistence, call recordings, transcript
persistence, credential database, billing, frontend call UI, production
Docker/FreeSWITCH deployment, provider live smoke tests, SaaS-OS changes. No
new database table, no new migration (`migrations/versions/` gained nothing
this phase). Tool definitions remain a small, statically registered,
code-owned set — no dynamic code execution from tenant configuration, no
generic "run arbitrary tool" escape hatch.

**One unrelated change was observed in `git status` and deliberately left
untouched**: `docs/PHASE-0-ARCHITECTURE.md` (a §23 addition) and a new
`docs/ADR/0010-one-frontend-multiple-user-contexts.md`, both timestamped
during this session but authored by neither this phase's brief nor this
implementation pass. Not reverted, not built upon, not otherwise touched —
flagged here for the reviewer's attention since it is outside this report's
own scope to explain.

## 15. Deviations from ADR-0003/Phase 2.2

1. **Service-account resolution is per-tenant, by name, not a fixed global
   id** — §5 above; a genuine gap in ADR-0003's own composition (point 2
   names `core.rbac.can()` but does not specify how the acting service
   account is resolved per tenant), caught and fixed during this phase's own
   integration testing, recorded as a Phase 2.4 addendum to ADR-0003.
2. **Idempotency uses a bounded, in-memory, per-call cache, not
   `core.idempotency.run_idempotent()`** — §6 above; a real shape mismatch
   between that primitive (synchronous, DB-transaction-scoped) and a tool
   handler's async `TelephonyProvider` work, explicitly permitted by this
   phase's own brief ("implement bounded per-call execution deduplication if
   that is consistent with the existing runtime architecture").
3. **ADR-0003 point 7's "persisted `ToolCall` record" is satisfied by
   `core.audit_log` entries, not a new table** — §8 above; required by this
   phase's own "no new database tables" constraint.
4. `CallTaskDependencies.on_tool_call_requested` (Phase 2.2's own documented
   placeholder for "a future tool request") is removed, replaced by
   `tool_gateway`/`telephony`/`system_service_account_name` — the exact,
   anticipated resolution Phase 2.2's own module docstring named this
   arrival as.

No other deviation. `TelephonyProvider`'s contract is unchanged (all four
methods this phase needed already existed); `ConversationEngine`,
`PipelinedEngine`, `CallSession`, `MediaProvider` and every API schema are
unchanged.

## 16. Known limitations

- **No tool other than the four call-control tools exists.** Contact/
  calendar/knowledge-style tools remain future phases, exactly as scoped.
- **No entitlement/quota gate.** ADR-0003 point 7's execution order names
  one between allowlist and schema validation; `core.usage`/`core.billing`
  integration is not part of this phase and the step is simply absent, not
  stubbed.
- **Idempotency is process-memory-scoped, not durable across a runtime
  crash** — §6/§15 above; an accepted, documented trade given Phase 2.2's
  own no-crash-takeover posture.
- **`scripts/bootstrap_rbac.py` still names its parameter
  `--service-account-id`**, provisioning one service account per tenant one
  at a time; it was not changed to auto-name every account
  `voiceagent-runtime` for the operator, since the brief scoped this phase
  to the gateway, not a bootstrap-tooling redesign — an operator must simply
  choose that name (or the configured `VOICEAGENT_RUNTIME_SYSTEM_SERVICE_
  ACCOUNT_NAME`) when calling `create_service_account()` for each tenant.
- No live telephony or real AI provider validation of any kind was performed
  or claimed — every test in this phase runs against `FakeTelephonyProvider`
  and, where an LLM is involved, `FakeLlmProvider`/`PipelinedEngine`'s own
  fakes.

## 17. Readiness for Phase 2.5

**READY.** The Tool Gateway foundation — registry, allowlist, authorization
(with the per-tenant service-account bug found and fixed against a real
database, not merely against mocks), idempotency, cancellation, audit, and
engine integration — is proven against fakes and a real PostgreSQL instance,
with all 364 hermetic and 48 integration tests green, zero lint/type/import
findings, and SaaS-OS untouched at its pinned SHA. A future phase adding
further tools (contact lookup, calendar, knowledge) needs only a new
`ToolDefinition` registration plus its own handler — no change to
`ToolGateway`, `CallRuntime`, `ConversationEngine`, `CallSession`, or any API
contract.
