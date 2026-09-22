# Phase 2.8 Status: Post-Call Analysis Foundation

## 1. Objective

Give the platform a small, durable, deterministic way to make completed
calls queryable: a `CallAnalysis` record, one per `CallSession`, holding
structured facts computed from data this product already persists --
turn/tool counts, duration, transfer/hold flags, contact association, and
outcome/follow-up references. A foundation for future summarization,
quality, and insight features -- not those features themselves (see §9
Non-Goals).

## 2. Implemented Components

- **`voiceagent.call_analysis`** -- a new domain package: `models.py`
  (`CallAnalysis`), `errors.py`, `permissions.py`, `service.py`
  (`build_call_analysis()`, `get_call_analysis()`).
- **Migration `0006_call_analysis`** -- `app.call_analysis`, RLS
  enable+force, tenant-safe composite FK, `UNIQUE(call_session_id)`.
- **Two REST routes** on the existing `voiceagent.api.v1.call_sessions`
  router: `GET`/`POST .../analysis[/rebuild]`.
- **RBAC**: `voiceagent.call_analysis` resource, `read`/`rebuild` actions.
- **One guarded integration point** in `voiceagent.runtime.call_task
  .run_call_task()`'s teardown: a best-effort automatic build after
  `CallSession` finalization (see §7).

No new Tool Gateway tool, no new package beyond `call_analysis`, no change
to any Phase 2.1–2.7 table or model.

## 3. Authoritative Source Domains

`CallAnalysis` is **never a second source of truth** (brief §4) -- every
field is a rebuildable snapshot; the table below is the exact, explicit
ownership split:

```text
CallAnalysis field                     authoritative source
--------------------------------------  -------------------------------------
turn_count/user_turn_count/...          voiceagent.conversations.ConversationTurn
duration_ms                             voiceagent.calls.CallSession.duration_ms
had_transfer/had_hold                   ConversationTurn (role='tool_call')
contact_associated                      voiceagent.calls.CallSession.contact_id
outcome                                 voiceagent.followups.CallOutcome.outcome
follow_up_count/appointment_.../open_.. voiceagent.followups.FollowUpAction
```

`CallSession` remains authoritative for lifecycle/timing, `ConversationTurn`
for conversation history, `CallOutcome` for business outcome,
`FollowUpAction` for follow-up state, `Contact` for identity, the calendar
domain for appointments. `voiceagent.call_analysis.service` never writes to
any of these five tables -- only reads them, and only ever writes to its own
`call_analysis` row.

## 4. CallAnalysis Model

```text
id, tenant_id, call_session_id, status,
turn_count, user_turn_count, assistant_turn_count,
tool_call_count, tool_result_count,
duration_ms (nullable), had_transfer, had_hold, contact_associated,
outcome (nullable), follow_up_count, appointment_follow_up_count,
open_follow_up_count,
created_at, updated_at
UNIQUE(call_session_id)
FK (call_session_id, tenant_id) -> call_sessions(id, tenant_id)
CHECK status IN ('pending', 'ready')
CHECK turn_count >= 0
```

`status` is a small, explicit two-value lifecycle (brief §3), not a
job/workflow state machine. This phase's own `build_call_analysis()` always
produces `"ready"` directly -- it is synchronous and deterministic, so there
is no in-flight state it could legitimately return in. `"pending"` is
declared and permitted by the `CHECK` only as a reserved value for a future
phase that separates *requesting* an analysis from *computing* it
asynchronously (see §8 Deviations) -- no code in this phase ever persists
it.

## 5. Metric Definitions

Stated exactly, with no semantic inference from text, matching brief §10:

- **`turn_count`** = every persisted `ConversationTurn` row for the call
  (all roles, including `system`).
- **`user_turn_count`/`assistant_turn_count`/`tool_call_count`/
  `tool_result_count`** = the same, filtered by `role`.
- **`had_transfer`/`had_hold`** = `True` iff at least one `tool_call` turn's
  `tool_payload["name"]` equals `"call.transfer"`/`"call.hold"` -- the exact
  tool ids `voiceagent.tools.handlers` registers those two Phase 2.4 tools
  under (verified directly against `TOOL_REGISTRY`, not just asserted as a
  literal --
  `tests/call_analysis/test_call_analysis_service.py
  ::test_transfer_and_hold_tool_ids_match_the_registered_tool_ids`). Never
  derived from parsing transcript text.
- **`contact_associated`** = `CallSession.contact_id IS NOT NULL`.
- **`outcome`** = `CallOutcome.outcome` for this call if one exists, else
  `None` -- never a sentinel string, never inferred.
- **`follow_up_count`** = every `FollowUpAction` row for the call.
  **`appointment_follow_up_count`** = the subset with `type ==
  'appointment'`. **`open_follow_up_count`** = the subset whose `status` is
  not in `voiceagent.followups.lifecycle.TERMINAL_STATUSES` -- reusing that
  module's own lifecycle definition, never a second one.
- **`duration_ms`** = `CallSession.duration_ms` verbatim -- `None` unless
  the call reached a terminal status with both `started_at`/`ended_at` set
  (`voiceagent.calls.service`'s own existing rule); never recomputed or
  invented here.

## 6. Generation Mechanism

`voiceagent.call_analysis.service.build_call_analysis(context,
call_session_id)`:

1. Validates the call belongs to `context.tenant_id` (RLS + explicit
   ownership check, same pattern as every other `voiceagent.*.service`
   lookup) -- raises `CallSessionNotFoundError` otherwise.
2. Computes every metric in §5 from a single `tenant_scope()` session.
3. **Upserts**: if a `CallAnalysis` row already exists for this call, its
   derived fields are updated in place; otherwise a new row is created.
   `uq_call_analysis_call_session` also enforces this at the database
   layer, so even a hypothetical concurrent double-build cannot produce two
   rows.
4. The whole read-compute-write happens inside one transaction (the same
   `tenant_scope()` session every other service in this codebase already
   uses for atomicity) -- either every derived field advances together, or
   none does.

No AI provider call, no network dependency, no direct database access from
the Tool Gateway (this phase adds no Tool Gateway tool at all) or an AI
engine -- `build_call_analysis()` is a plain, synchronous application
service, reachable only through `tenant_scope()`, exactly like
`voiceagent.calendars.service`/`voiceagent.followups.service`.

## 7. Automatic Timing

Integrated at the post-call completion boundary, using the existing bounded
mechanism (brief §6) -- no new job/background framework. In
`voiceagent.runtime.call_task.run_call_task()`'s `finally` block, **after**
`CallSession` finalization (so `duration_ms`/`ended_at` are already set),
one additional step:

```python
try:
    await deps.db.run(build_call_analysis, context, call_session_id)
except Exception:
    _logger.exception(...)
```

This crosses the exact same `voiceagent.runtime.db.DatabaseBoundary` seam
every other write in this function already uses (`transition_call_session`,
`get_call_session`, `get_agent_version`, `authorize_call_data_access`) --
not a new mechanism. It runs strictly after the audio pump has already
stopped (`_run_pumps()` itself is untouched -- still imports nothing
database-related, still passes
`tests/architecture/test_runtime_db_boundary.py` unchanged), so **it never
blocks the audio path**. A build failure is logged and swallowed, never
allowed to crash call teardown or another call's task -- always
recoverable later through `POST /v1/call-sessions/{id}/analysis/rebuild`.
Verified against real PostgreSQL: `tests/integration
/test_runtime_integration.py::test_run_call_task_happy_path_completes_and_finalizes`
(unmodified) still passes with this step now executing for real on every
run.

This runs even on the authorization-denied early-return path (`finalized =
True`), producing a trivial all-zero analysis for a call that never
started its engine -- a correct, harmless outcome, not a special case
worth branching on.

## 8. API

```text
GET  /v1/call-sessions/{id}/analysis          -- read-only, never triggers a build
POST /v1/call-sessions/{id}/analysis/rebuild  -- explicit, idempotent rebuild
```

Both nested on the existing `voiceagent.api.v1.call_sessions` router,
matching Phase 2.6/2.7's own precedent of adding a call-scoped sub-resource
route directly there. `404` for both "no such call" and "call exists but
has no analysis yet" -- no response distinguishes the two, matching every
other not-found convention in this codebase (`CallOutcomeNotFoundError`,
`FollowUpActionNotFoundError`, ...). No tenant ID is ever accepted from a
request body -- `context: TenantContext` is resolved exclusively through
the existing `require_tenant()` dependency chain, exactly like every other
route in this codebase.

**Rebuild is a separate, explicit permission from `read`** (brief §8):
justified because outcome/follow-up data routinely changes *after* a call
ends -- that is the entire design point of the follow-up domain -- so the
snapshot taken automatically at call-end is frequently stale for exactly
those fields; an authorized caller can refresh it on demand. The default,
primary-use API is the read route; rebuild exists for that one concrete,
documented reason, not speculatively.

## 9. Explicit Non-Goals (confirmed absent)

No AI summarization, no autonomous post-call LLM workflow, no generic
analytics/event-processing framework, no generic workflow engine, no
generic event bus, no background task framework, no message queue, no
sentiment analysis, no transcription service, no CRM, no marketing
automation, no arbitrary user-defined formulas, no new Tool Gateway tool,
and no change to SaaS-OS, previous migrations, or any Phase 2.1–2.7 model.

## 10. Security / RLS

`app.call_analysis`: RLS `ENABLE` + `FORCE`, verified against real
PostgreSQL (`tests/integration/test_domain_rls_integration.py
::test_row_level_security_is_enabled_and_forced_for_every_table`, extended
this phase to list all eleven tenant-owned tables). Composite tenant-aware
FK into `call_sessions`; a cross-tenant `call_session_id` fails at the
database constraint layer (`IntegrityError`), verified directly. Tenant
isolation verified for both the service layer and the two route functions
called directly (`tests/integration/test_call_analysis_integration.py`).
Real RBAC verified: an unbootstrapped actor is denied (`core.rbac.can()`
returns `False`, never raises); `bootstrap_tenant_rbac()` grants both
`voiceagent.call_analysis` actions (implicitly proven by the existing,
unmodified `tests/integration/test_rbac_bootstrap_integration.py
::test_bootstrap_registers_and_grants_every_declared_permission`, which
asserts `result.granted == PERMISSIONS` and now includes the two new
entries automatically). No raw conversation content is ever logged --
`build_call_analysis()`'s own failure path logs only `call_session_id`,
matching the finalize step immediately above it in `call_task.py`.

## 11. Tests

**Hermetic** (`pytest`, no PostgreSQL): 491 passed (baseline 479 + 12 new:
`tests/call_analysis/` (9: model shape/constraints, the pure `_tool_name()`
helper), plus 3 new migration-shape assertions in `tests/test_migrations.py`).

**Integration** (`pytest -m integration`, real PostgreSQL 16): 141 passed
(baseline 117 + 24 new, `tests/integration/test_call_analysis_integration.py`,
covering: deterministic metric computation for every rule in §5 against
real persisted data, idempotent rebuild with no duplicate row, rebuild
refreshing stale values, tenant isolation, cross-tenant FK rejection,
not-found before any build, the two API routes called directly -- the same
technique `tests/integration/test_conversation_integration.py`'s own
`get_conversation_route` tests already establish -- and real RBAC
authorization), plus one pre-existing test updated for the new table (§12).

**Total: 632 passed, 0 failed.** No existing test was removed or weakened;
the one changed pre-existing test is documented in §12 with why.

## 12. Quality Gates

All run and green in this environment:

- `ruff check .` -- clean.
- `ruff format --check .` -- clean.
- `pyright` -- 0 errors, 0 warnings.
- `lint-imports` -- 8 contracts kept, 0 broken (including after the
  `voiceagent.runtime.call_task` edit -- `tests/architecture
  /test_runtime_db_boundary.py` still passes unmodified: the audio pump
  itself was never touched, only `run_call_task()`'s own `finally` block,
  which already imports and calls several `voiceagent.*.service` functions
  the same way).
- `detect-secrets scan` on every file this phase added or modified -- 0
  findings (the one pre-existing false positive at `tests/test_migrations.py`
  line 25, an unchanged dummy URL, predates this phase; `.secrets.baseline`
  was left exactly as it was at the start of this phase).
- `pytest` (hermetic) and `pytest -m integration` (real PostgreSQL) -- both
  green, see §11.
- No frontend files were touched; the frontend check was not run.
- Migration validated: `alembic heads` shows exactly one head
  (`0006_call_analysis`); `alembic upgrade head --sql` renders cleanly
  offline and was also applied for real against the test PostgreSQL
  instance.

## 13. Deviations

- **`ANALYSIS_STATUSES` includes `"pending"`, which this phase's own code
  never persists.** Declared and `CHECK`-permitted for forward
  compatibility with a future phase that separates request-from-computation
  (brief §3 lists `pending`/`ready` as the example lifecycle without
  requiring both be reachable within this phase); documented rather than
  silently omitted or silently left unused without explanation.
- **No dedicated `TestClient`-level HTTP tests** were added for the two new
  routes. This is not a new deviation -- it continues the exact convention
  Phase 2.1/2.6/2.7 already established (no domain route in this codebase
  has HTTP-level tests); instead, the two route *functions* are called
  directly with a real `TenantContext` inside the integration suite, the
  same technique `tests/integration/test_conversation_integration.py`'s
  `get_conversation_route` tests already use -- a stronger proof than a
  mocked `TestClient` call would give, since it runs the real service and a
  real database underneath.
- **`voiceagent.call_analysis` is a new top-level package**, not folded
  into `voiceagent.calls` or `voiceagent.followups` -- it derives from
  four different domains at once (calls, conversations, followups,
  contacts), so it does not belong to any single one of them; a dedicated
  package keeps the "who owns what" boundary explicit rather than implying
  false ownership by whichever package happened to host it.

## 14. Limitations

- The automatic post-call build happens exactly once, at call teardown.
  Any outcome/follow-up change made after that point (the common case --
  follow-ups are frequently created or completed well after a call ends)
  requires an explicit `POST .../analysis/rebuild` to be reflected; nothing
  currently re-triggers it automatically on a later outcome/follow-up
  write, by design (brief §13: no generic event bus was introduced to wire
  that up).
- `had_transfer`/`had_hold` only see transfer/hold actions taken through
  the Tool Gateway's `call.transfer`/`call.hold` tools (the only mechanism
  that can transfer or hold a call in this product) -- there is no other
  path for either action to happen undetected.

## 15. Phase 2.9 Readiness

`CallAnalysis` gives a future summarization/quality/insight phase a
deterministic, already-computed baseline to build from (turn/tool counts,
duration, outcome/follow-up shape) without needing to re-derive any of it
from raw conversation data itself. A future phase adding an async job
framework for heavier, non-deterministic analysis (e.g. LLM summarization)
can introduce `status = 'pending'` as a real, observed state at that point,
using the value this phase already reserved rather than a schema change.
