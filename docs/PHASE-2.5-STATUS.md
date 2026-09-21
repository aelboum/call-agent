# Phase 2.5 — Durable Call State & Conversation History: STATUS

**Status**: IMPLEMENTATION COMPLETE — AWAITING REVIEW (implementation
complete, not committed)
**Updated**: 2026-09-22
**SaaS-OS pin**: `ff550010e5eafecace7311038aadc99fcecfbe3d` (unchanged,
verified)

This document records what Phase 2.5 actually built: durable call-history
persistence (reusing the existing `app.call_sessions` model unchanged) and a
new durable, provider-neutral conversation-turn model
(`app.conversation_turns`), wired into the running call through a bounded,
off-audio-path persistence component, with a minimal tenant-authorized read
API.

---

## 1. Objective

Persist the minimum durable information required to understand what
happened during a call after the runtime process has ended, without ever
making persistence part of the real-time audio hot path.

## 2. Durable call model

**Unchanged.** `voiceagent.calls.models.CallSession` (Phase 2.1) already
carries every lifecycle field the brief's section 1 asks for: `started_at`,
`answered_at`, `ended_at`, `duration_ms`, `status` (the seven-state machine
in `voiceagent.calls.lifecycle`, matching the brief's own list exactly),
`tenant_id`, `agent_version_id`, `phone_number_id`, `runtime_instance_id`/
`runtime_assigned_at`, and normalized `hangup_cause`/`end_reason`. No second
call table was created; no column was added. `voiceagent.runtime.call_task
.run_call_task()` already persists every lifecycle transition through
`DatabaseBoundary.run(transition_call_session, ...)`, at call-start, answer,
in-progress, and teardown — never inside `_run_pumps()`. This was already
true before Phase 2.5 and required no change.

## 3. Conversation model

New table: `app.conversation_turns`
(`voiceagent/conversations/models.py`, `migrations/versions
/0003_create_conversation_turns.py`). One row per durable, provider-neutral
turn:

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | PK |
| `tenant_id` | `uuid` | FK `core.tenants.id` |
| `call_session_id` | `uuid` | composite FK `(call_session_id, tenant_id) -> (call_sessions.id, call_sessions.tenant_id)`, `ON DELETE CASCADE` |
| `event_id` | `varchar(128)` | stable idempotency key (§8) |
| `sequence` | `integer` | per-call monotonic order (§5) |
| `role` | `varchar(20)` | `system` \| `user` \| `assistant` \| `tool_call` \| `tool_result` |
| `content` | `text`, nullable | finalized text, for the three text roles |
| `tool_payload` | `json`, nullable | structured `{"name","arguments"}`/`{"value","error_code"}`, for the two tool roles |
| `created_at`/`updated_at` | `timestamptz` | `TimestampMixin` |

A `CHECK` constraint enforces the content/tool_payload split at the database
layer (`ck_conversation_turns_payload_shape`), not merely by application
convention.

**No `conversations` wrapper table.** Phase 2.0's report (§11.2) forecast one
alongside `conversation_turns`; Phase 2.5 does not build it.
`voiceagent/conversations/models.py`'s own module docstring records why:
this product has no concept of a conversation independent of the call that
carries it — one `CallSession` is always exactly one conversation, with no
multi-session continuation or merge. A wrapper table whose only content
would be `(id, tenant_id, call_session_id)` duplicates `CallSession` for no
independent data, which the brief's own section 9 rules out ("do not
introduce a generic multi-purpose `events` table merely because it appears
convenient" / "do not add tables for future features"). `call_session_id`
is this table's own conversation identity. `tests/test_migrations.py
::test_deferred_domain_tables_are_never_created` still asserts `conversations`
itself is never created, now as a deliberate, documented permanent decision
rather than "not yet."

Vendor SDK types never appear here: `tool_payload` carries `{"name",
"arguments"}` (from the product-owned `ToolCallRequested`) or `{"value",
"error_code"}` (from the product-owned `ToolResult`) — never a raw
OpenAI-compatible message object, never a provider transcript event.

## 4. Ordering model

`sequence` is a plain per-call `Integer`, **not** a PostgreSQL
`IDENTITY`/`SERIAL` column. `voiceagent.conversations.service
.persist_conversation_turn()` computes it in Python from the small,
per-call set of already-persisted sequence numbers — the same "no
`sqlalchemy.func.max()`" discipline `voiceagent.agents.service
.create_draft_version()` already applies to `AgentVersion.version_number`
(ADR-0007 point 2) — made atomic against a concurrent writer for the same
call by a `SELECT ... FOR UPDATE` lock on the owning `CallSession` row, the
identical primitive `voiceagent.calls.service.claim_runtime_ownership()`
already uses for its own read-then-write invariant. See ADR-0007's new
"Phase 2.5 addendum" for the full reasoning.

Independently, at the runtime layer: `voiceagent.runtime
.conversation_persistence.ConversationPersistence` gives each call exactly
one bounded `asyncio.Queue` and exactly one FIFO consumer task
(`_CallWorker`). A tool call's own "tool_call" turn is always enqueued
*before* the task that awaits its dispatch is even created
(`voiceagent.runtime.call_task._run_pumps()`'s `pump_engine_events()`), and
its "tool_result" turn is enqueued only after that dispatch resolves — so
FIFO queue order alone guarantees the brief's own ordering requirement
(§5: "assistant tool-call message persisted before tool result") even before
either row reaches the database. The database-level lock above is defense
in depth for a future multi-worker scenario this phase does not itself
create, not the only thing making today's ordering correct.

Never timestamp-ordered: two turns persisted within the same clock tick
still sort deterministically by `sequence`.
`voiceagent.conversations.service.list_conversation_turns()` always orders
by `sequence ASC`.

## 5. Persistence architecture

```text
pump_engine_events()  (voiceagent.runtime.call_task, audio-adjacent)
    |
    | persist_turn(PendingTurn(...))     synchronous, non-blocking put_nowait
    v
_CallWorker's own bounded asyncio.Queue  (one per call, maxsize=256 default)
    |
    | a single dedicated asyncio.Task, FIFO
    v
DatabaseBoundary.run(persist_conversation_turn, ...)   off the event loop
```

`voiceagent.runtime.conversation_persistence.ConversationPersistence` is a
process-wide component (constructed once, exactly like `voiceagent.tools
.gateway.ToolGateway`), but all of its real state is per-call: `start()`
creates one `_CallWorker`, `finish()` tears it down. `voiceagent.runtime
.call_task.run_call_task()` calls `deps.conversation_persistence.start(...)`
right after `deps.engine.start(...)` (so the engine's own session-start
events — `SystemPromptSet`, and an `AssistantResponse` for the configured
greeting, if any — are never dropped for lack of a worker) and
`await deps.conversation_persistence.finish(call_session_id)` in the same
`finally` block that already calls `deps.tool_gateway.forget_call(...)`.

`_run_pumps()` never crosses `DatabaseBoundary` or any database name
itself — `persist_turn` is a `Callable[[PendingTurn], None]` closure,
exactly the same shape `dispatch_tool_call` already is for the Tool
Gateway. `tests/architecture/test_runtime_db_boundary.py` (extended this
phase to include `persist_conversation_turn` in its forbidden-name set) and
the new `tests/architecture/test_conversation_persistence_isolation.py`
both assert this mechanically.

## 6. Persistence failure behavior

```text
runtime call continues
        +
persistence failure recorded/logged (event_id, role, call_session_id only —
                                       never turn content or tool arguments)
        +
bounded retry (default: 3 retries, linear backoff starting at 0.5s)
        +
final failure observable (one ERROR-level log line; WorkerStats.failed
                            incremented, queryable via
                            ConversationPersistence.stats_for())
```

No infinite retry queue, no second durable queue, no Kafka/RabbitMQ/event
bus — the brief's own explicit non-goals. `_CallWorker.enqueue()` is
non-blocking (`asyncio.Queue.put_nowait()`); a full queue drops the new
turn, logs a warning (`WorkerStats.dropped`), and returns `False` — it
never blocks the caller and never grows the queue past
`RuntimeSettings.conversation_persistence_queue_size` (default 256).

**Durability guarantee, stated honestly**: a conversation turn is durable
once its own database transaction commits. Before that point — while
queued, while retrying, or if the process crashes mid-retry — it is
runtime-process memory only, exactly the same boundary Phase 2.4's Tool
Gateway idempotency cache already has (`docs/PHASE-2.4-STATUS.md` §6), and
for the identical reason: Phase 2.2 implements no crash takeover (ADR-0008
point 10), so a crashed process leaves no call task alive to ever redeliver
a dropped or lost turn. A gap in one call's conversation history is a real,
possible outcome of a sufficiently persistent database outage — it is
never silent (every drop and every final failure is logged), and it never
causes the call itself to fail, hang, or retry differently.

## 7. Runtime persistence architecture (bounded lifecycle)

Every `_CallWorker` has:

* **Bounded lifecycle** — created by exactly one `start()` call, torn down
  by exactly one `finish()` call, both from `run_call_task()`'s own
  well-defined points.
* **Cancellation behavior** — `finish()` sends a sentinel, then
  `asyncio.wait_for(self._task, timeout=drain_timeout_seconds)`
  (`RuntimeSettings.conversation_persistence_drain_timeout_seconds`, default
  5.0s); on timeout the task is cancelled by `wait_for` itself.
* **Clear ownership** — one worker per `call_session_id`, held in
  `ConversationPersistence._workers`, never shared across calls.
* **Deterministic shutdown** — `finish()` always returns within
  `drain_timeout_seconds`, regardless of database or persist-function state;
  proven by `tests/runtime/test_conversation_persistence
  ::test_finish_is_bounded_even_when_the_worker_is_permanently_stuck`.

`tests/runtime/test_conversation_persistence.py` proves queue-full behavior
never blocks the caller (`test_queue_full_drops_the_turn_and_never_blocks
_the_caller`), so the "never allow an unbounded queue" requirement is
proven, not merely asserted by code review.

## 8. Idempotency

`event_id` is a stable, provider-neutral identifier every persistable
`EngineEvent` now carries, generated **once**, at event construction, in
`voiceagent.providers.engines.contracts` (`FinalTranscript.event_id`,
`SystemPromptSet.event_id`, `AssistantResponse.event_id`, each via a
`field(default_factory=...)` — never regenerated per persistence retry,
since a retry re-persists the *same* Python event object). `ToolCallRequested
.call_id`/`ToolResult.call_id` (Phase 1/2.2, unchanged) serve the identical
role for the two tool roles — they were already the Tool Gateway's own
idempotency key (Phase 2.4), reused here rather than duplicated.

`persist_conversation_turn()` is idempotent under `UNIQUE(call_session_id,
event_id, role)` (both an application-level check and a database
constraint, §4): a second call with the same `(call_session_id, event_id,
role)` returns the already-persisted row, consumes no new `sequence`
number, and creates no second row. `role` is part of the key, not just
`event_id`, precisely because `ToolCallRequested.call_id`/`ToolResult
.call_id` are reused as one shared `event_id` for a tool call's two
different turns (below) — without `role`, persisting the "tool_result"
turn would be indistinguishable from a duplicate delivery of the
"tool_call" turn and would be silently dropped. This was a real defect
this phase's own integration testing caught against a real PostgreSQL
instance (a hermetic test using a fake `persist_fn` could not have caught
it, since the collision is a database-constraint-and-query-shape issue),
not merely designed around in the abstract — see `migrations/versions
/0003_create_conversation_turns.py`'s own module docstring for the exact
failure observed before this fix.

**Tool Gateway integration**: `voiceagent.tools.gateway.ToolGateway`'s own
Phase 2.4 idempotency cache (per-call, in-memory, keyed by
`ToolCallRequested.call_id`) already prevents a duplicate *execution* of a
tool handler for a retried `call_id` — a cached `ToolResult` is returned
without re-running the handler. Because `voiceagent.runtime.call_task
._run_pumps()` persists the "tool_result" turn once per `ToolResult` it
sees emitted from `_execute_and_submit_tool_call()`, and a cached replay
still produces exactly one `ToolResult` per dispatch, no duplicate
"tool_result" turn is ever queued for the same `call_id` — and even if it
were, `persist_conversation_turn()`'s own idempotency check would still
reject the duplicate insert. Two independent layers, not one relying on
the other.

## 9. Database schema / migration

`migrations/versions/0003_create_conversation_turns.py`. Follows every
established convention from `0002_create_agent_phone_call_tables.py`:
schema `app`, composite tenant-aware FK into `call_sessions`, `tenant_id`
index, `tenant_rls_statements()` (`ENABLE` + `FORCE` + policy), explicit
`GRANT`. New this phase: `ON DELETE CASCADE` on the composite FK (§10) and
two `UNIQUE` constraints (`(call_session_id, event_id, role)`,
`(call_session_id, sequence)`) plus a `CHECK` enforcing the content/
tool_payload shape per role.

**Two real defects were found and fixed during this phase's own
integration testing against a real PostgreSQL 16 instance, not left for a
reviewer to find**:

1. `app.call_sessions` (migration `0002`, Phase 2.1) never carried
   `UNIQUE(id, tenant_id)` — nothing had referenced it as a composite-FK
   parent before Phase 2.5. `CREATE TABLE app.conversation_turns` failed
   with `there is no unique constraint matching given keys for referenced
   table "call_sessions"`. Fixed by adding that constraint as this
   migration's own first step (`0002` itself, already-applied committed
   history, is not edited retroactively).
2. `ConversationTurn.tool_payload`'s ORM column was declared as bare
   `JSON`. SQLAlchemy's own default for a JSON-typed column
   (`none_as_null=False`) binds a Python `None` as the *JSON* literal
   `null`, not SQL `NULL` — every system/user/assistant insert (whose
   `tool_payload` is `None`) then violated
   `ck_conversation_turns_payload_shape`'s own `tool_payload IS NULL`
   clause. Fixed with `JSON(none_as_null=True)` on the model column (a
   Python/ORM-layer fix only — the migration's physical `JSON` column type
   needed no change).

## 10. Retention / deletion

`voiceagent.conversations.service.delete_conversation_turns(context,
call_session_id)` deletes every turn for one call, scoped by both
`tenant_id` and `call_session_id` in the `DELETE` statement itself (RLS is
a second, independent enforcement of the same boundary, not the only one),
using the sanctioned `voiceagent.db.delete()` bulk primitive (ADR-0007:
"a bulk `DELETE FROM <table> WHERE ...` over an ORM-mapped table expresses
no function call"). Returns the row count deleted; `0` for an empty call
is not an error.

No `CallSession`-deleting service exists anywhere in this codebase, so no
current code path can orphan a `conversation_turns` row via a deleted
parent — the `ON DELETE CASCADE` on the composite FK is documented,
forward-looking defense for the day a future phase adds one, not a
behavior exercised by any test today (recorded honestly, not tested with a
fabricated scenario).

No retention *scheduler* was built — the brief's own explicit boundary
("do not implement a complete retention scheduler yet"). A future phase can
call `delete_conversation_turns()` from a scheduled job with no change to
this module.

## 11. API

`voiceagent/api/v1/conversations.py`:

* `GET /v1/call-sessions/{call_session_id}/conversation` — ordered
  (`sequence ASC`) durable turns for one call. 404 for a foreign or
  nonexistent call (§12).

`voiceagent/api/v1/call_sessions.py` (extended, not replaced):

* `GET /v1/call-sessions` — now paginated (`limit`, default 50, max 200;
  `offset`) and filterable (`status`, one of the seven lifecycle states),
  newest-first (`created_at DESC, id DESC` — deterministic pagination).
  An unrecognized `status` value returns an empty page rather than a 422:
  there is no information-disclosure or correctness reason to distinguish
  that from a real status with zero matching rows.
* `GET /v1/call-sessions/{call_session_id}` — unchanged (call detail).

`voiceagent.calls.service.list_call_sessions()` gained the `status`/
`limit`/`offset` keyword parameters backing the route; every argument is
optional and defaults to the original Phase 2.1 unfiltered/unlimited
behavior, so its one other production shape (none currently — it has no
other caller) is unaffected.

Never exposed: `runtime_instance_id`, `runtime_assigned_at`,
`fs_channel_uuid`, `data_authorization_decision_id` (internal runtime
ownership/audit-correlation metadata, excluded from `CallSessionOut` since
Phase 2.1, unchanged), raw provider payloads, or any tool argument beyond
what the tenant's own `ConversationTurn.tool_payload` already durably
holds for their own call.

## 12. Authorization

Reuses `voiceagent.tenancy.require_tenant()` exactly (no second permission
system). A new resource, `voiceagent.conversations.permissions.RESOURCE =
"voiceagent.conversations"`, deliberately distinct from
`voiceagent.calls.permissions.RESOURCE` (`"voiceagent.call_sessions"`):
conversation *content* is more sensitive than call *metadata* (§13), so a
role can be granted read access to one without the other. Registered into
`voiceagent.rbac_bootstrap.PERMISSIONS` (one more `(resource, action)`
tuple entry) and `register_permissions()` — no broad grant to every role,
the same bootstrap discipline every prior phase's permission followed.

`voiceagent.conversations.service.list_conversation_turns()` raises
`CallSessionNotFoundError` (the identical class/behavior
`voiceagent.calls.service.get_call_session()` already uses) for a call
that does not belong to the authenticated tenant — mapped to the identical
404 the `call-sessions` detail route already returns for a foreign
`CallSession`. A call session id alone is never sufficient: authorization
requires `authenticated identity + tenant context + permission
(RBAC check via require_tenant) + call ownership (the tenant-scoped lookup
inside list_conversation_turns())`, in that order, before any row is
returned. `tests/integration/test_conversation_integration
.py::test_a_call_session_id_alone_is_not_sufficient_to_read_a_conversation`
proves this against a real, foreign, real-owner tenant.

## 13. Privacy / logging behavior

No conversation `content` or `tool_payload` value appears in any log line
this phase writes. `_CallWorker`'s own failure logging
(`voiceagent.runtime.conversation_persistence`) carries only
`call_session_id`, `event_id`, and `role` — never `turn.content` or
`turn.tool_payload`, checked by direct code inspection of every
`_logger.warning`/`_logger.exception` call site in that module (none
formats `turn.content` or `turn.tool_payload`). `core.audit_log` entries
this phase's code touches (none directly — the Tool Gateway's own Phase
2.4 audit entries, unchanged, already excluded tool arguments) remain
exactly as narrow as `docs/PHASE-2.4-STATUS.md` §8 already established.

## 14. Call lifecycle integration

Unchanged from Phase 2.2/2.4 (§2 above) — `initiated -> answered ->
in_progress -> {completed | failed | interrupted}` via `voiceagent.calls
.service.transition_call_session()`, called from `run_call_task()`'s own
start/authorize/teardown points, never inside `_run_pumps()`. A process
crash mid-call still cannot leave a false "completed" row: `run_call_task
()`'s own `finally` block (unmodified in its own transition logic this
phase) always finalizes to a real terminal status before returning, and
`voiceagent.runtime.reconciliation` remains the documented recovery path
for a runtime whose heartbeat has actually expired.

## 15. ConversationEngine integration

`voiceagent.providers.engines.contracts` gained two new event types and one
new field, kept product-owned and framework-free like the rest of the
contract:

* `FinalTranscript.event_id: str` (new field, `default_factory`) — the
  finalized caller-utterance turn now carries a stable idempotency key.
* `SystemPromptSet(instructions, event_id)` (new event) — the durable
  "system" turn, emitted once at session start.
* `AssistantResponse(text, event_id)` (new event) — the durable
  "assistant" turn, distinct from `AudioOut`: it carries the text the agent
  decided to say, once, independent of how many `AudioOut` frames its
  synthesis produces or whether synthesis happens at all.

`voiceagent.providers.engines.pipelined.PipelinedEngineSession` emits
`SystemPromptSet` (and an `AssistantResponse` for the configured greeting,
if any) at construction, and an `AssistantResponse` for each completed
non-tool-call turn, right before its own synthesis loop. No SQL, no
`voiceagent.conversations` import, and no `voiceagent.db` import appears
anywhere under `voiceagent/providers/engines/` — mechanically proven by the
new "The ConversationEngine never imports conversation persistence"
import-linter contract and `tests/architecture
/test_conversation_persistence_isolation.py`'s matching AST scans, mirroring
the identical Tool Gateway fence Phase 2.4 established. `voiceagent.tools`
adapters (Gemini/Mistral/Groq/Deepgram/AssemblyAI/ElevenLabs/Deepgram
Aura) were not touched.

## 16. Tool Gateway integration

Already covered by §5 (ordering) and §8 (idempotency) above. Concretely,
`voiceagent.runtime.call_task._run_pumps()`:

1. On `ToolCallRequested`, enqueues a `"tool_call"` turn
   (`tool_payload={"name", "arguments"}`) *before* creating the task that
   dispatches it.
2. Awaits `dispatch_tool_call(request)` (the Tool Gateway, or a fake in
   hermetic tests).
3. Enqueues the matching `"tool_result"` turn
   (`tool_payload={"value", "error_code"}`) using the *normalized*
   `ToolResult` — the same one `_execute_and_submit_tool_call()`'s own
   `except` clause already builds for an unhandled dispatch exception, so a
   Tool Gateway crash still produces a durable, non-leaking `tool_result`
   turn (`{"value": null, "error_code": "internal_error"}`), never a raw
   traceback.
4. Only then calls `engine_session.submit_tool_result(result)`.

`tests/integration/test_tool_gateway_integration
.py::test_run_call_task_dispatches_a_real_tool_call_end_to_end` (extended
this phase) proves this against the real `ToolGateway`, real PostgreSQL,
and real audit log — not only against fakes.

## 17. Tests and exact counts

Run via `.venv/Scripts/python.exe -m pytest` (default, hermetic) and
`.venv/Scripts/python.exe -m pytest -m integration` (real PostgreSQL 16 +
Redis). Exact counts are reported in the accompanying implementation
report, not duplicated here to avoid the two ever silently drifting apart —
see that report's own "Tests and exact counts" section for the authoritative
numbers from the actual run this phase performed.

New/changed test files:

* `tests/conversations/test_conversation_turn_models.py` — hermetic schema
  shape (role/idempotency/ordering/cascade/payload-shape constraints), no
  database.
* `tests/runtime/test_conversation_persistence.py` — hermetic: FIFO
  ordering, tool_call-before-tool_result at this layer, queue-full drop
  behavior (never blocks the caller), bounded retry then permanent
  failure, transient-failure recovery, bounded/deterministic `finish()`
  under a permanently stuck persist function, idempotent `start()`/
  `finish()`, observable stats.
* `tests/runtime/test_call_task_persistence.py` — hermetic: `_run_pumps()`
  wiring for `SystemPromptSet`/`FinalTranscript`/`AssistantResponse`,
  tool_call-before-tool_result ordering, normalized-failure tool_result
  persistence, partial-transcript/`TurnEnded` exclusion, and
  backward-compatible `persist_turn=None` default (every Phase 2.4
  `test_call_task_tools.py` test keeps passing unmodified).
* `tests/architecture/test_conversation_persistence_isolation.py` — the
  `voiceagent.conversations`-scoped import fences and the reverse-direction
  "ConversationEngine never imports conversation persistence" fence.
* `tests/architecture/test_runtime_db_boundary.py` — extended with
  `persist_conversation_turn` in the audio-pump forbidden-name set.
* `tests/providers/test_pipelined_engine.py` — two pre-existing tests
  updated for the new session-start `SystemPromptSet`/greeting-
  `AssistantResponse` events (`test_full_turn_pipeline_ordering`,
  `test_close_is_idempotent_and_ends_the_event_stream`); every other test
  in this file was unaffected (they assert with `any(isinstance(...))`,
  not exact-index checks).
* `tests/test_migrations.py` — `conversation_turns` removed from the
  "deferred tables" list (with a new, permanent-deferral note for
  `conversations`); two new assertions that the table and its RLS now
  exist.
* `tests/integration/test_conversation_integration.py` (new) — real
  PostgreSQL: RLS enable/force, cross-tenant isolation (both at the raw-row
  level and through `list_conversation_turns()`), monotonic/unique/gapless
  ordering, idempotent duplicate persistence, invalid-role rejection,
  foreign-call-session fail-closed persistence and read, empty-conversation
  read, deletion (full, empty, and does-not-touch-another-call's-turns),
  and the API route functions directly (ordered read, 404 for a foreign
  call, completed/failed call reads, call-history pagination and status
  filtering, unknown-status-returns-empty).
* `tests/integration/test_tool_gateway_integration.py` — extended:
  `CallTaskDependencies` call site now includes a real
  `ConversationPersistence`; the existing full-dispatch test now also
  waits for and asserts the durable `tool_call`-before-`tool_result`
  ordering and payload shape from the real run.
* `tests/integration/test_runtime_integration.py` — four
  `CallTaskDependencies` call sites updated with a real
  `ConversationPersistence` (required field; no behavioral test change).

## 18. Static analysis results

Reported in the accompanying implementation report (ruff check, ruff
format --check, pyright, lint-imports, detect-secrets, frontend checks) —
this document states the architecture; that report states the actual
command output from the run this phase performed.

## 19. SaaS-OS

`ff550010e5eafecace7311038aadc99fcecfbe3d`, matching the installed
package's `direct_url.json` and `pyproject.toml` — unchanged from Phase
2.4. Consumed only as an external pinned dependency; untouched, unforked,
uncopied, unmodified.

## 20. Deviations from the brief

1. **No separate `conversations` table** — §3 above; the brief's own
   section 9 ("do not add tables for future features that are not part of
   this phase") applies directly once `call_session_id` is recognized as
   this product's own conversation identity.
2. **`sequence` is a Python-computed `Integer` under a row lock, not a
   database `IDENTITY`/`SERIAL`** — §4 above and ADR-0007's new addendum;
   required by this product's own pre-existing "no `sqlalchemy.func`"
   discipline, not a brief requirement being loosened.
3. **The bounded persistence mechanism is a plain `asyncio.Queue` + one
   FIFO task per call**, not a more general worker-pool or batched-write
   design — the brief's own "implement the smallest bounded mechanism
   necessary" instruction (section 7), and the identical shape Phase 2.4's
   Tool Gateway idempotency cache already chose for an analogous
   per-call-lifetime, in-memory bound.
4. **`Settings.list_call_sessions()`'s new `status`/`limit`/`offset`
   parameters are all optional**, preserving the exact Phase 2.1 call shape
   for any future caller that does not pass them — an explicit, minimal
   extension rather than a breaking signature change, though this phase's
   brief did not require backward compatibility here (there was no other
   caller to break).

No other deviation. `CallSession`, `voiceagent.calls.lifecycle`, the Tool
Gateway's own execution order and audit behavior (Phase 2.4), and every API
schema predating this phase are unchanged.

## 21. Known limitations

* **Persistence is not durable across a runtime crash mid-retry or
  mid-queue** — §6 above; an accepted, documented trade given Phase 2.2's
  own no-crash-takeover posture, identical in shape to the Tool Gateway's
  own Phase 2.4 limitation.
* **No retention scheduler** — §10 above; `delete_conversation_turns()`
  exists as the application-service seam a future phase's scheduled job
  calls into, not itself a scheduler.
* **No streaming/partial-transcript persistence** — deliberate (brief
  section 4). A future phase adding live transcript display would extend
  the product-owned contract again (as this phase did for
  `SystemPromptSet`/`AssistantResponse`), not reach around it.
* **`RealtimeEngine`/`FakeRealtimeProviderSession` emit no
  `SystemPromptSet`/`AssistantResponse` of their own** — Phase 2.5's brief
  explicitly excludes realtime-provider work; a future realtime adapter
  emits these same contract events when that work happens, exactly the
  same way `PipelinedEngineSession` does today.
* **No object storage, no recordings, no raw audio persistence of any
  kind** — the brief's own explicit non-goal (section 3); nothing in this
  phase's code path touches `voiceagent.providers.objectstore` or any
  audio buffer beyond what already existed.
* **`ConversationPersistence`'s bounded queue size, retry count/backoff,
  and drain timeout are conservative Phase 2.5 defaults**
  (`RuntimeSettings.conversation_persistence_*`), not benchmarked
  production values — the same "needs a benchmark against real load"
  caveat `RuntimeSettings`'s own module docstring already states for
  `to_thread_pool_size`/`max_concurrent_calls` (ADR-0008 point 15, OQ-1
  remains open).

## 22. Readiness for Phase 2.6

**READY.** Durable call history (reusing `CallSession` unchanged) and
durable, ordered, idempotent, provider-neutral conversation turns are
proven against real PostgreSQL, with the audio/media pump proven
(mechanically, by two independent enforcement layers) never to touch the
database directly. A future phase adding richer conversation features
(streaming partial-transcript display, call recordings/object storage,
AI-generated summaries) needs only its own new event type or its own new
table — no change to `ConversationEngine`, `CallTaskDependencies`'s
existing fields, `ToolGateway`, or any API contract this phase established.
