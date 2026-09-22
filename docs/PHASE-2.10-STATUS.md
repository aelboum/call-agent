# Phase 2.10 Status: Controlled Call Workflows

## 1. Objective

Add a small, bounded primitive that lets a published agent configuration
describe a deterministic sequence of call actions, executable during a live
call. Explicitly not a generic workflow engine, DAG platform, or scheduler
-- see section 17.

## 2. Architecture

```text
AgentVersion.config["workflow"]          (voiceagent.workflows.config.WorkflowDefinition,
    |                                     immutable once the AgentVersion is published -- ADR-0004)
    v
Tool Gateway "workflow.advance" tool     (voiceagent.tools.handlers._workflow_advance)
    |
voiceagent.workflows.executor.run_workflow()
    |
    +-- voiceagent.workflows.service       (claim/advance/complete/fail CallWorkflowExecution;
    |                                        evaluate_condition; apply_outcome_step/apply_follow_up_step
    |                                        -- reuses voiceagent.followups.service verbatim)
    +-- ToolGateway.execute() (recursive)   (a `tool` step's only path to another Tool Gateway tool --
                                              same allowlist/RBAC/idempotency/audit as any model-issued call)
```

The workflow *definition* lives inside the existing, immutable
`AgentVersion.config` JSON column -- there is no independent workflow
version/lifecycle table; it inherits publish-time immutability for free
(ADR-0004). The workflow *executor* is reached only as one more Tool
Gateway tool (`workflow.advance`), dispatched off the audio pump exactly
like every other tool call (`voiceagent.runtime.call_task
._execute_and_submit_tool_call()`'s own `asyncio.create_task()`) -- it
never runs inside the raw audio/media callback path, and the
`ConversationEngine` never imports it (verified by import-linter and the
existing AST fences; see section 9).

**No new engine-contract event type was added.** The LLM can only ever
reference the workflow through the single, fixed, argument-less
`workflow.advance` tool name -- it can never name a step, a predicate, a
tool id, or any other part of the graph. `run_workflow()` walks the entire
bounded step graph server-side in one pass (condition/tool/outcome/
follow_up steps require no further model input), stopping at an `end` step,
a failed step, cancellation, or `MAX_EXECUTION_TRANSITIONS`.

## 3. Workflow Model

`voiceagent.workflows.config.WorkflowDefinition`:

```python
class WorkflowDefinition:
    entry_step_id: str
    steps: list[WorkflowStep]   # discriminated union on `type`, 1-20 entries
```

Owned by `voiceagent.agents.config.AgentConfig.workflow: WorkflowDefinition | None`
-- read/write/publish already exist (`voiceagent.agents.service
.create_draft_version()`/`publish_version()`, `POST /v1/agents/{id}/versions`,
`POST /v1/agents/{id}/versions/{version_id}/publish`); Phase 2.10 adds no new
agent-configuration API. The key invariant this satisfies: a `CallSession`
always executes against the deterministic, immutable workflow snapshot on
its own `agent_version_id` -- a later draft edit can never reach an
already-running call.

## 4. Supported Step Types

Exactly five (brief SCOPE), each with a stable `step_id` and an explicit
transition to another `step_id` (`end` has none, by construction):

| Type        | Effect                                             | Reuses                                    |
|-------------|-----------------------------------------------------|--------------------------------------------|
| `tool`      | Invoke one existing, registered Tool Gateway tool    | `ToolGateway.execute()` (recursive, depth 1) |
| `condition` | Branch on one predicate (`PREDICATES`)               | `voiceagent.workflows.predicates`          |
| `outcome`   | Set the call's business outcome                      | `voiceagent.followups.service.set_call_outcome()` |
| `follow_up` | Create a `contact`/`manual_follow_up` follow-up       | `voiceagent.followups.service.create_follow_up()` |
| `end`       | Terminate the workflow                                | --                                          |

**Predicates** (`voiceagent.workflows.predicates.PREDICATES`, nine members):
`contact_associated`/`contact_not_associated`, `outcome_exists`/
`outcome_not_exists`, `transfer_occurred`, `hold_occurred`,
`follow_up_exists`, `appointment_exists`, `call_duration_at_least_seconds`
(the one predicate taking a bounded `threshold_seconds`, 0-86400). All nine
are evaluated against already-persisted call state read fresh at evaluation
time (`CallSession`, `CallOutcome`, `FollowUpAction`, `ConversationTurn`) --
the same authoritative sources `voiceagent.call_analysis.service
.build_call_analysis()` documents, never a stale `CallAnalysis` snapshot
(which only exists after the call ends). There is no expression language:
a `condition` step names exactly one predicate from this closed set and two
next-step ids.

**`follow_up` steps are deliberately narrower than `voiceagent.followups
.models.FOLLOW_UP_TYPES`** -- only `contact`/`manual_follow_up`, never
`appointment` (which needs runtime-supplied calendar parameters a static,
published step cannot sensibly provide). A workflow that needs to schedule
an appointment reaches `call.create_follow_up`/`calendar.create_appointment`
through a `tool` step instead.

## 5. Validation Rules

Structural validation runs once, at parse time, via one `pydantic
.model_validator` on `WorkflowDefinition` (`voiceagent.workflows.config`):

- unknown step type (pydantic discriminated union on `type`)
- malformed step configuration (missing/extra fields, `extra="forbid"`)
- duplicate `step_id`
- unknown `entry_step_id`
- invalid transition (a `next`/`if_true`/`if_false` naming a step that does
  not exist)
- missing terminal path (no `end` step present)
- unreachable step (not reachable by BFS from `entry_step_id`)
- cycle (DFS from `entry_step_id`)
- excessive step count (`Field(max_length=MAX_WORKFLOW_STEPS=20)`)
- unsupported predicate (`Literal` type)
- inconsistent predicate/threshold (`call_duration_at_least_seconds`
  requires `threshold_seconds`; every other predicate forbids it)
- unknown outcome value / unsupported follow-up type
- excessive/malformed tool-step arguments (at most 10 flat JSON-primitive
  entries; no nested structures)
- a `tool` step naming `workflow.advance` itself (no nesting)

A `WorkflowDefinition` that fails any of these fails the surrounding
`AgentConfig` the same way any other malformed field does
(`pydantic.ValidationError`, Phase 2.1's own established boundary, a 422 at
`POST /v1/agents/{id}/versions`) -- there is no separate "validate workflow"
endpoint.

**Tool existence is checked separately**, by
`voiceagent.workflows.validation.validate_tool_references()`, called from
`voiceagent.agents.service.create_draft_version()` -- deliberately *not* a
`WorkflowDefinition` pydantic validator. Reason: `voiceagent.agents.config`
(which owns `AgentConfig.workflow`) is imported by `voiceagent.providers
.engines.factory` (for `EngineSelection`), and that module must never
transitively reach the Tool Gateway or the database (import-linter's "The
ConversationEngine never imports the Tool Gateway"/"...conversation
persistence" contracts, both of which a naive `TOOL_REGISTRY`-checking
validator on `WorkflowDefinition` would have broken -- caught by `lint-
imports` during this phase's own implementation, see section 9). A local,
literal copy of `voiceagent.followups.models.OUTCOME_VALUES`'s values
(`voiceagent.workflows.config._OUTCOME_VALUES`) exists for the identical
reason -- importing `voiceagent.followups.models` would pull in
`voiceagent.db`. Both duplications are guarded by a dedicated hermetic test
that fails if the two ever drift.

## 6. Execution Engine

`voiceagent.workflows.executor.run_workflow()`: one bounded pass per
`workflow.advance` tool call.

1. `load_workflow_definition(agent_version)` -- re-parse `config["workflow"]`
   (pure, no DB). `WorkflowNotConfiguredError`/`WorkflowDefinitionInvalidError`
   (the latter defensive: a published version's config already passed
   validation once and cannot change).
2. `begin_execution()` -- claim/create the call's one
   `CallWorkflowExecution` row (section 8's idempotency guard). A
   redelivered claim for an already-terminal execution returns it unchanged
   (never re-run); a redelivered claim while one is still `running` raises
   `WorkflowExecutionInProgressError`.
3. Loop, up to `MAX_EXECUTION_TRANSITIONS` (= `MAX_WORKFLOW_STEPS` = 20)
   iterations: resolve the current step, dispatch by type, persist the
   transition (`record_step_advance()`), advance `current_step_id`. Stops
   at an `end` step (`complete_execution()`), a failed `tool` step
   (`fail_execution(reason="tool_step_failed")`), an ownership conflict
   (`fail_execution(reason="execution_conflict")`), or the ceiling
   (`fail_execution(reason="max_steps_exceeded")`).
4. `asyncio.CancelledError` is caught, `cancel_execution()` is attempted
   best-effort, and the exception is always re-raised -- never suppressed.

Because `WorkflowDefinition`'s own structural validator already guarantees
an acyclic, fully-reachable step graph of at most 20 nodes, no real
execution can ever need more than 20 transitions -- `MAX_EXECUTION_TRANSITIONS`
is enforced as defense-in-depth against a future validator bug, not the
expected stop condition (proven directly, bypassing the validator, in
`tests/workflows/test_executor.py::test_max_execution_transitions_is_enforced`).

A `tool` step's failure (`ToolResult.error_code is not None`) is reported
as a normal `status="failed"` **value**, never a raised exception at the
model boundary (ADR-0003 point 8) -- only a genuine invocation-level
problem (no workflow configured, one already running, a corrupted
definition) raises `ToolExecutionError` from the handler.

## 7. LLM Boundary

The LLM can only ever call the fixed, argument-less `workflow.advance` tool
-- it never names a workflow, a step, a predicate, a tool id, or supplies
any payload the executor interprets. Every step's shape is fixed at
publish time by the tenant/operator who authored the `AgentVersion`, never
by model output at runtime. A `tool` step's `arguments` are the *step
author's* static values, not model-supplied. There is no path from model
output to: SQL, HTTP, Python, a dynamically chosen service class, the Tool
Gateway allowlist, RBAC, or tenant isolation -- every `tool` step still
crosses `ToolGateway.execute()` in full (allowlist check, RBAC check,
idempotency, audit), exactly as a model-issued call would.

## 8. Persistence / Execution State

One new table, `app.call_workflow_executions`
(`voiceagent.workflows.models.CallWorkflowExecution`,
`migrations/versions/0008_create_call_workflow_executions.py`):

| Column                 | Purpose |
|-------------------------|---------|
| `tenant_id`             | RLS scope |
| `call_session_id`       | `UNIQUE` -- the idempotency guard (section 10) |
| `agent_version_id`      | which published snapshot governed this run |
| `workflow_config_hash`  | copy of `AgentVersion.config_hash` at claim time |
| `status`                | `running` / `completed` / `failed` / `cancelled` |
| `current_step_id`       | progress, updated every transition |
| `steps_executed`        | bounded by `MAX_EXECUTION_TRANSITIONS` |
| `started_at`/`ended_at` | `ended_at` set iff `status` is terminal (`CHECK`) |
| `failure_reason`        | closed vocabulary, never a raw exception message |

RLS: `ENABLE`+`FORCE ROW LEVEL SECURITY` (via `infra.db.tenant_rls_statements()`,
the established mechanism -- a model never grants or weakens it).
Tenant-safe composite foreign keys to `app.call_sessions` and
`app.agent_versions` (both `(id, tenant_id)`), matching the pattern every
other product table in this schema already uses. No cross-tenant reference
is representable: the FK itself rejects it, proven directly against a real
Postgres instance (`tests/integration/test_workflow_execution_integration.py`).

No generic `jobs`/`tasks`/`events`/scheduler table was added -- this is one
narrow, purpose-built table for one purpose (execution-progress tracking
and the idempotency guard), not a reusable job-queue primitive.

**Recovery boundary, stated plainly**: if the runtime process crashes
mid-execution, the row is left `status='running'` forever -- there is no
lease/reclaim mechanism, deliberately, because a crashed runtime process
has also lost the call itself (ADR-0008/OQ-4: no crash takeover of a live
call exists in this product today). This mirrors the identical, already-
accepted boundary `voiceagent.tools.gateway.ToolGateway`'s own in-memory,
per-call idempotency cache documents. See section 17.

## 9. Transaction / Concurrency Behavior

Every database-touching step is one short, separate `DatabaseBoundary.run()`
call -- claim, then (for a `tool` step) the external `ToolGateway.execute()`
await happens with **no transaction open**, then the outcome is persisted
in a second, separate call. No lock is ever held across an `await`.

**Import-boundary finding, corrected during this phase's own implementation**:
an earlier draft had `voiceagent.workflows.config.WorkflowDefinition`
import `voiceagent.tools.registry`/`voiceagent.followups.models` directly
for its own validation. `lint-imports` caught the resulting violation --
`voiceagent.providers.engines.factory` (which legitimately imports
`voiceagent.agents.config` for `EngineSelection`) would have transitively
reached `voiceagent.tools`/`voiceagent.db`, breaking "The ConversationEngine
never imports the Tool Gateway"/"...conversation persistence". Fixed by
moving the tool-existence check to a separate module
(`voiceagent.workflows.validation`, called only from `voiceagent.agents
.service`, which the engine factory does not import) and by duplicating the
small outcome-value vocabulary locally rather than importing it. All eight
`import-linter` contracts are KEPT with this codebase as of this phase.

## 10. Retry / Idempotency Behavior

`uq_call_workflow_executions_call_session` is the durable idempotency
primitive: at most one execution row per call, ever.
`begin_execution()` claims it transactionally (check-then-insert, with the
`UNIQUE` constraint itself as the authoritative race-loser signal --
`IntegrityError` -> `WorkflowExecutionInProgressError` -- for the window
between a concurrent read and insert, exactly one caller's `INSERT`
succeeds). A redelivered `workflow.advance` for a call whose execution
already reached a terminal status is answered from that row, unchanged,
never re-executing a single step -- proven directly in
`tests/workflows/test_executor.py::test_redelivered_terminal_execution_is_idempotent`
and `tests/integration/...::test_begin_execution_is_idempotent_for_a_redelivered_terminal_row`.
`outcome`/`tool` steps are naturally idempotent (they reuse already-
idempotent services/tools); a `follow_up` step is not (each execution of it
creates one follow-up) -- this cannot be reached twice through the normal
path (one claim, one pass), and is the one concrete instance of section 8's
"no crash takeover" boundary that a mid-execution crash could in principle
leave inconsistent (documented, not silently accepted -- section 17).

## 11. Security / RLS / RBAC

- RLS + FORCE RLS on `call_workflow_executions` (section 8).
- Tenant-safe composite FKs; no cross-tenant reference representable.
- `voiceagent.workflows.permissions`: one resource
  (`voiceagent.call_workflows`), one action (`read`) -- the read-only
  execution-status route only. No `execute`/`advance`/`run` permission
  exists: `workflow.advance` is authorized exactly like every other Tool
  Gateway tool, through `voiceagent.tools.permissions`'s existing,
  `TOOL_REGISTRY`-derived permission set (already included in
  `voiceagent.rbac_bootstrap.PERMISSIONS`) -- no new end-user "execute
  arbitrary workflow" capability was introduced.
- A `tool` step can never invoke a tool the agent/tenant is not permitted
  to use: the nested `ToolGateway.execute()` call runs the identical
  allowlist + RBAC + idempotency + audit gate a model-issued call would
  (`ToolExecutionContext.tool_gateway`/`agent_version`/
  `system_service_account_name`, the three fields this phase adds to that
  dataclass specifically so a workflow-tool-step handler can re-enter the
  gateway rather than bypass it).
- Malformed workflow rejection: section 5.
- Audit: `workflow.execution_started`/`_completed`/`_failed`/`_cancelled`
  (`core.audit_log`, `ActorType.SYSTEM`), each carrying only stable
  identifiers and bounded, closed-vocabulary metadata (`agent_version_id`,
  `steps_executed`, `failure_reason`) -- never conversation text, secrets,
  full tool payloads, or exception stack traces. A nested `tool` step's own
  audit trail is `ToolGateway`'s existing, unmodified one.

## 12. API

One new route: `GET /v1/call-sessions/{id}/workflow-execution` (read-only,
`voiceagent.call_workflows:read`) -- mirrors the existing
`GET /v1/call-sessions/{id}/analysis` precedent. No route exists to
trigger, cancel, or retry an execution directly, no route accepts a
caller-supplied step graph for execution, and no generic
workflow-management surface was added: reading and publishing a workflow
*definition* already goes through the existing `voiceagent.api.v1.agents`
routes (`POST .../versions`, `POST .../versions/{id}/publish`), unchanged
by this phase.

## 13. Bounds (code-enforced, not merely documented)

| Bound | Value | Enforced by |
|---|---|---|
| Max steps per workflow | 20 | `WorkflowDefinition.steps` (`Field(max_length=...)`) |
| Max execution transitions | 20 | `voiceagent.workflows.executor.MAX_EXECUTION_TRANSITIONS` |
| Max tool-step argument entries | 10 | `WorkflowToolStep._bound_arguments()` |
| Max condition-duration threshold | 86400s (24h) | `WorkflowConditionBranch.threshold_seconds` (`Field(le=...)`) |
| Workflow nesting | 0 (forbidden) | `WorkflowToolStep._validate_tool_id()` rejects `workflow.advance` |
| `workflow.advance` execution timeout | 45s | `ToolDefinition.timeout_seconds`, enforced by the existing `ToolGateway._run_handler()` |

## 14. Test Results

Hermetic (`pytest -q`, no PostgreSQL/Redis/network): **all passing**
(see section 16 for the exact command and result). New coverage:

- `tests/workflows/test_workflow_config.py` -- structural validation: legal
  graphs exercising every step type, unknown step type, duplicate/unknown
  step ids, missing terminal, unreachable steps, two cycle shapes, step
  limit (at and over), unsupported predicate, predicate/threshold
  consistency, unknown outcome value, unsupported follow-up type, argument
  bounds (count and nested-structure rejection), frozen-instance
  immutability, `PREDICATES`/`_OUTCOME_VALUES` sync guards.
- `tests/workflows/test_validation.py` -- tool-existence check.
- `tests/workflows/test_predicates.py` -- every predicate, pure.
- `tests/workflows/test_executor.py` -- every step type end-to-end, both
  condition branches, tool-step failure, not-configured, already-running,
  redelivered-terminal idempotency, mid-run ownership conflict,
  cancellation (recorded + re-raised), max-transitions ceiling (via a
  deliberately-oversized fake definition bypassing the real validator, to
  prove the executor's own ceiling is independent).
- `tests/tools/test_gateway.py` -- new `ToolExecutionContext` fields
  (`agent_version`/`tool_gateway`/`system_service_account_name`) carried
  through; `workflow.advance` registered.
- `tests/tools/test_registry.py` -- updated built-in-tool-count assertion
  (ten -> eleven).
- `tests/test_migrations.py` -- table creation, RLS+FORCE, the
  one-per-call `UNIQUE` constraint, no second `workflows`/
  `workflow_versions` table.

Integration (`pytest -m integration`, requires real PostgreSQL --
**not runnable in this sandboxed environment**; written and reviewed
against the same conventions `tests/integration/test_call_outcomes_followups_integration.py`
already establishes, execution deferred to an environment with Postgres):
`tests/integration/test_workflow_execution_integration.py` -- migration
round-trip, RLS/tenant isolation, cross-tenant `CallWorkflowExecution`/
`CallSession` FK rejection, `begin_execution()`'s idempotent-redelivery and
concurrent-duplicate-refusal behavior, `workflow.advance` executing
end-to-end through the real `ToolGateway` (including a nested `call.hold`
tool call) under real RBAC, not-configured/not-allowlisted/unauthorized
failure paths.

## 15. Quality Gates

| Gate | Result |
|---|---|
| Hermetic test suite (`pytest -q`) | **pass** (all tests, no failures) |
| Integration suite (`pytest -m integration`) | not run -- no PostgreSQL instance available in this environment (see `tests/integration/README.md`); written, not executed |
| `ruff check` | **pass**, 0 findings |
| `ruff format --check` | **pass**, all files already formatted |
| `pyright` | **pass**, 0 errors/warnings |
| `import-linter` (`lint-imports`) | **pass**, 8/8 contracts KEPT |
| `alembic upgrade head --sql` (offline) | **pass** -- single head, targets `app` schema, grants issued, no `core` schema touched, `call_workflow_executions` created exactly once with RLS+FORCE |
| `detect-secrets scan` (this phase's added/modified files) | **0 new findings**. A repo-wide `detect-secrets scan --baseline .secrets.baseline` run mutates `.secrets.baseline` with the same pre-existing placeholder-credential noise every prior phase's status doc documents (Phase 2.1 through 2.9) -- reverted, not a real finding. The one finding a scoped scan of this phase's files surfaces is the **pre-existing** dummy `Basic Auth Credentials` string at `tests/test_migrations.py:25` (`postgresql+psycopg://owner:unused@127.0.0.1:1/unused`), already present before this phase and explicitly preserved, not introduced by it. |

## 16. SaaS-OS / Environment

SaaS-OS pin: `ff550010e5eafecace7311038aadc99fcecfbe3d`, unchanged --
verified via `pip show saas-os`/the pinned checkout; no file under any
SaaS-OS package was read, written, or otherwise touched by this phase.
`pyproject.toml` unchanged -- no new dependency was needed. No Phase 2.11+
work was started.

## 17. Known Limitations / Deferred

- **No crash-recovery resume** for a mid-execution `CallWorkflowExecution`
  (section 8/10) -- the accepted boundary already established by
  `voiceagent.tools.gateway.ToolGateway`'s own in-memory idempotency cache
  and by ADR-0008/OQ-4 (no crash takeover of a live call exists yet). A
  crash mid-`follow_up`-step could in principle leave a duplicate follow-up
  on a later, different execution attempt; there is no later attempt in
  practice today (a crashed runtime's calls are never resumed at all), so
  this is a real but currently unreachable gap, not a silent one.
- **`workflow.advance` runs the whole bounded pass in one call**, not
  step-by-step re-entry through the model. This was a deliberate
  simplification (section 2) -- every step type this phase supports is
  non-conversational (no step needs the model's own judgment mid-graph);
  a future phase that wants a step requiring live model input would need a
  different mechanism, not an extension of this one.
- **No workflow-execution cancellation API** beyond the existing call-task
  cancellation path (a hangup/runtime-shutdown cancels the in-flight
  `workflow.advance` tool call exactly like any other tool call) -- no
  dedicated `POST .../workflow-execution/cancel` route exists, and none
  was requested.
- **Deferred, explicitly, per the brief's STRICT NON-GOALS**: a generic
  workflow engine, an arbitrary DAG platform, a visual workflow builder,
  CRM/marketing/email/SMS automation, campaigns, generic webhooks or HTTP
  actions, arbitrary code/expression execution, user-defined plugins, cron
  or recurring workflows, a generic scheduler or job queue, Redis as
  workflow truth, leader election, distributed locking, MCP, a frontend
  workflow builder, Phase 2.11 agent knowledge/context, and Phase 2.12
  advanced post-call intelligence. None of these exist anywhere in this
  phase's changes.
