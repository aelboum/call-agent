# Phase 2.9 Status: Safe Scheduled Follow-Up Execution

## 1. Objective

Extend the existing `FollowUpAction` domain (Phase 2.7) into a small,
reliable execution lifecycle: safely determine which follow-ups are due,
and execute them through narrowly defined application services, correct
under retries, duplicate execution attempts, cancellation, concurrent
workers, process failure, and tenant isolation.

`FollowUpAction` remains the **one and only** authoritative table -- no
second follow-up table, no generic workflow/scheduler/job-event framework
(brief §3/§18).

## 2. Implemented Components

- **`voiceagent.followups.lifecycle`** (extended) -- `"processing"`/
  `"failed"` join the Phase 2.7 vocabulary; full transition table below.
- **`voiceagent.followups.retry_policy`** (new) -- pure, DB-free: which
  types are auto-executable, the bounded retry ceiling, the exponential
  backoff formula, the claim lease duration, the closed failure-reason
  vocabulary.
- **`voiceagent.followups.models`** (extended) -- six new columns on
  `follow_up_actions`: `attempt_count`, `next_attempt_at`,
  `last_attempted_at`, `completed_at`, `failure_reason`, `execution_id`.
- **`voiceagent.followups.service`** (extended) -- `claim_due_follow_up()`,
  `complete_follow_up_execution()`, `fail_follow_up_execution()`,
  `execute_due_follow_up()`, `reprocess_follow_up()`,
  `list_follow_ups_by_status()`; `create_follow_up()` seeds the new claim
  column, `cancel_follow_up()` widens to `failed -> cancelled` and gains a
  conditional audit entry.
- **`voiceagent.followups.worker`** (new) -- `FollowUpWorker`: the bounded
  `asyncio` polling loop that calls `execute_due_follow_up()`.
- **Migration `0007_extend_follow_up_execution`** -- widens
  `ck_follow_up_actions_status`, adds the six columns, two new CHECK
  constraints, one partial index.
- **RBAC**: one new `voiceagent.follow_up_actions:retry` permission.
- **API**: `GET /v1/follow-ups` (tenant-scoped, status-filterable) and
  `POST /v1/follow-ups/{id}/retry`, both on the existing router.

No change to `voiceagent.tools` (Tool Gateway) or `voiceagent.runtime`
(call audio path) -- see §12/§13.

## 3. Lifecycle

```text
pending    -> processing | completed | cancelled
processing -> completed | failed
failed     -> processing | cancelled
completed  -> (terminal)
cancelled  -> (terminal)
```

`pending -> completed`/`pending -> cancelled` are Phase 2.7's own edges,
unchanged -- `complete_follow_up()`/`cancel_follow_up()` still reach them
directly for a follow-up type with no concrete execution service
(`'contact'`/`'manual_follow_up'`, see §9). `processing -> completed |
failed` and `failed -> processing` (retry, automatic or administrative) are
new. `failed -> cancelled` is new too: an operator can give up on a failed
follow-up before its retries are exhausted, matching "a cancelled follow-up
is never executable" (brief §4) without forcing a wait on
`retry_policy.MAX_ATTEMPTS`.

`TERMINAL_STATUSES` is unchanged (`{"completed", "cancelled"}`) -- not
reused blindly, but re-derived: it is still exactly the set of statuses
with no outgoing edge. `"processing"`/`"failed"` are not terminal, even
though a `"failed"` row that has exhausted `MAX_ATTEMPTS` is *practically*
terminal (never claimed again) -- that exhaustion is retry-policy
bookkeeping (`attempt_count` vs. `MAX_ATTEMPTS`), not a distinct lifecycle
status.

A redelivered transition to the current status (including
`processing -> processing`, a stale-lease reclaim) is a no-op, matching
`voiceagent.calls.lifecycle`'s own rule.

## 4. Scheduling

`due_at` (Phase 2.7, reused per brief §4 rather than duplicated) remains
the caller's own, immutable-after-creation "when should this happen" value.
`create_follow_up()` seeds a new internal column, `next_attempt_at`, from
`due_at` -- but only for a follow-up whose `type` is in
`retry_policy.EXECUTABLE_TYPES`; every other follow-up's `next_attempt_at`
stays `NULL` forever (never auto-claimed).

`next_attempt_at` is the *one* column `claim_due_follow_up()`'s due-lookup
query reads, and it serves three purposes by construction, never by a
compound `OR` filter:

| Row state                          | `next_attempt_at` meaning                     |
| ----------------------------------- | ---------------------------------------------- |
| `pending`, never claimed            | `due_at` (copied in at creation)               |
| `processing`, lease active          | `now + retry_policy.LEASE_SECONDS` (claim time) |
| `failed`, retries remain            | `now + retry_policy.next_attempt_delay_seconds(attempt_count)` |
| `failed`, `MAX_ATTEMPTS` exhausted  | `NULL` (never claimed again)                    |
| `completed` / `cancelled`           | `NULL`                                          |
| type not in `EXECUTABLE_TYPES`      | `NULL` (always)                                 |

A follow-up is claimable only when `next_attempt_at IS NOT NULL AND
next_attempt_at <= now()`. This gives the four required semantics directly:
not executable before `due_at`; never executable once cancelled or
completed (`next_attempt_at` cleared on both transitions); retried only
per the explicit backoff. No cron expression, no recurrence: `due_at` is a
single point in time, `create_follow_up()` takes no interval/recurrence
parameter, and nothing re-derives a "next occurrence."

## 5. Execution Types

`retry_policy.EXECUTABLE_TYPES = frozenset({"appointment"})`. Only
`'appointment'` has a concrete application service to execute against
(`voiceagent.calendars.service`); `'contact'`/`'manual_follow_up'` have
none, so `create_follow_up()` never seeds `next_attempt_at` for them and
`claim_due_follow_up()`'s query never matches them. They remain fully
usable follow-ups -- created, listed, manually completed or cancelled via
the existing Phase 2.7 API -- just never picked up by
`FollowUpWorker`. This is a deliberate, documented non-goal (brief §5), not
an oversight.

**What "executing" an appointment follow-up means.** The appointment's
`calendar_event_id` is already fixed at `create_follow_up()` time (brief
§8's own relationship rule,
`ck_follow_up_actions_appointment_requires_calendar_event`) -- execution
never creates or duplicates a `CalendarEvent`. `_execute_appointment_follow_up()`
calls `calendar_service.get_event()` to confirm the linked event still
stands: `CalendarEventNotFoundError` or `status == 'cancelled'` fails the
attempt (bounded-retried); otherwise the follow-up is marked `completed`.
No calendar write happens during execution at all.

## 6. Claim / Concurrency

`claim_due_follow_up(context, *, now=None)`:

1. `SELECT ... FROM follow_up_actions WHERE tenant_id = :t AND type IN
   (:executable_types) AND status IN ('pending', 'processing', 'failed')
   AND next_attempt_at IS NOT NULL AND next_attempt_at <= :now ORDER BY
   next_attempt_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED` -- one atomic
   PostgreSQL row lock. A concurrent caller racing for the same row skips
   it (already locked) and, if nothing else is eligible, gets `None`;
   `tests/integration/test_follow_up_execution_integration.py::
   test_two_concurrent_claims_never_claim_the_same_row` verifies this
   against a real PostgreSQL instance with two real worker threads.
2. Transition to `"processing"`, `attempt_count += 1`,
   `last_attempted_at = now`, `next_attempt_at = now + LEASE_SECONDS`,
   `execution_id` generated once if unset.
3. Commit (the `tenant_scope()` transaction ends here) -- the claim is
   fully durable before any external call is made.
4. `execute_due_follow_up()` then performs `calendar_service.get_event()`
   (§5) with **no database transaction open**.
5. `complete_follow_up_execution()`/`fail_follow_up_execution()` open a
   second, separate transaction for the final transition.

No database transaction is ever held open across the external call (brief
§6).

## 7. Idempotency

`execution_id` is a stable UUID, generated once on a follow-up's first
claim and unchanged by every later attempt or stale-lease reclaim -- "a
stable execution/idempotency identity associated with the FollowUpAction"
(brief §7). `complete_follow_up_execution()`/`fail_follow_up_execution()`
both require the caller's `execution_id` to match the row's *current* one
and the row to still be `status='processing'`; a mismatch raises
`FollowUpExecutionConflictError` rather than overwriting a later attempt's
outcome (the case where this claim's lease already expired and a second
worker reclaimed the row first).

Because execution for the one implemented type is read-only against the
calendar (§5), retrying it is *unconditionally* idempotent: there is no
second idempotency-key mechanism to build or integrate with, because there
is no write to make idempotent. `tests/integration/
test_follow_up_execution_integration.py::
test_retried_execution_never_creates_a_duplicate_calendar_event` monkeypatches
`calendar_service.create_event` to raise if called at all, then executes
the same follow-up twice (including once after a simulated failure), and
asserts it is never invoked. A future execution type that *does* write
(explicitly out of scope this phase, brief §18) would carry `execution_id`
as the idempotency key into that write.

## 8. Retries / Failure

`retry_policy.MAX_ATTEMPTS = 5`. `fail_follow_up_execution()` sets
`next_attempt_at = now + retry_policy.next_attempt_delay_seconds(attempt_count)`
(exponential, `BACKOFF_BASE_SECONDS=60` doubling, capped at
`BACKOFF_CAP_SECONDS=3600` -- the same formula
`infra.jobs.queue._with_retry_and_dead_letter()` already uses, not a second
shape) while attempts remain, or `next_attempt_at = None` once
`attempt_count >= MAX_ATTEMPTS` -- the row stays `status='failed'` forever,
visible and reportable, never claimed again automatically. No infinite
retry loop; deterministic given a fixed `attempt_count` (pure function,
hermetically unit-tested in `tests/followups/test_retry_policy.py`).

`failure_reason` is one of a closed, four-value vocabulary
(`retry_policy.FAILURE_REASONS`) -- `calendar_event_not_found`,
`calendar_event_cancelled`, `max_attempts_exceeded` is not currently
produced automatically (exhaustion is silent, by design: the row's own
`attempt_count`/`MAX_ATTEMPTS` already say so) but is reserved vocabulary,
and `unexpected_error` for anything else -- never a raw exception message
or provider response (brief §8/§17), enforced by both a CHECK constraint
and `fail_follow_up_execution()` itself.

## 9. Stale-Processing Recovery

A `"processing"` row's `next_attempt_at` is set to `now + LEASE_SECONDS`
(`retry_policy.LEASE_SECONDS = 300`) at claim time. While the lease is
active, the row's `next_attempt_at` is in the future, so
`claim_due_follow_up()`'s own query excludes it -- an active execution is
never stolen. Once the lease expires (the worker that claimed it crashed,
or its process was killed, before calling `complete_follow_up_execution()`/
`fail_follow_up_execution()`), the row becomes claimable again through the
exact same query path, with the same `execution_id` retained and
`attempt_count` incremented again. No lease-renewal heartbeat, no
distributed leader election, no Redis-as-source-of-truth: PostgreSQL alone
decides eligibility, exactly as brief §9 requires. `tests/integration/
test_follow_up_execution_integration.py` covers both directions: a stale
lease is safely reclaimed, and an active one is not.

## 10. Execution Service

`voiceagent.followups.service`: `claim_due_follow_up()`,
`complete_follow_up_execution()`, `fail_follow_up_execution()`,
`execute_due_follow_up()` (the one function that composes the other three
plus the calendar dispatch), `reprocess_follow_up()`. All live inside the
follow-up/application domain -- no execution logic in
`voiceagent.tools` or `voiceagent.runtime`.

## 11. Worker

`voiceagent.followups.worker.FollowUpWorker` -- the smallest explicit
polling abstraction, deliberately not built on `infra.jobs` (ARQ/Redis):
that module is a generic enqueue/execute/retry/dead-letter runner, but the
actual hard problem here (atomic claim, database-held lease, deterministic
backoff) has to live in PostgreSQL regardless of what triggers a poll, and
no SaaS-OS primitive enumerates tenants across the Row-Level Security
boundary for a Redis-triggered handler to use (the identical, already-
documented structural limit `voiceagent.runtime.reconciliation` states for
itself). One bounded `asyncio` loop calling `execute_due_follow_up()` is
the entire mechanism this domain needs (brief §18: not a generic job/event
framework).

- **Tenant enumeration is the caller's job** (`tenant_ids: Callable[[],
  Sequence[uuid.UUID]]`, an operator-supplied directory) -- mirroring
  `reconcile_tenant()`'s own documented scope limit exactly.
- **Bounded concurrency**: up to `max_concurrent_tenants` (default 4)
  tenants drained concurrently via an `asyncio.Semaphore`; each tenant
  claims at most `max_claims_per_tenant_per_tick` (default 10) follow-ups
  per tick. No unbounded task creation.
- **Graceful shutdown/cancellation**: `start()`/`shutdown()` mirror
  `CallRuntime.start_heartbeat()`/`shutdown()` exactly -- idempotent start,
  `task.cancel()` + awaited suppression of `CancelledError` on shutdown.
- **Database access**: exclusively through its own
  `voiceagent.runtime.db.DatabaseBoundary` instance (a generic, reusable
  bounded-thread-pool wrapper, not audio-specific) -- never the call
  runtime's own instance, never a blocking call on any event loop.
- **Does not share the call audio execution path**: imports nothing from
  `voiceagent.runtime` beyond `db.DatabaseBoundary`, is never constructed
  by `CallRuntime`, and one tenant's unexpected error is caught and
  isolated per tenant per tick (never aborts another tenant's own tick or
  crashes the loop).

## 12. API

- `GET /v1/follow-ups?status=<status>` -- tenant-scoped, bounded (200 rows),
  most-overdue-first. New (brief §12).
- `GET /v1/call-sessions/{id}/follow-ups` -- unchanged (call-scoped).
- `POST /v1/follow-ups/{id}/retry` -- new, the one administrative execution
  control this phase exposes.
- `POST /v1/follow-ups/{id}/complete`, `POST /v1/follow-ups/{id}/cancel` --
  unchanged.

No `execute`/`claim` route exists or ever will through this API surface --
`FollowUpWorker` is the only caller of `claim_due_follow_up()`/
`execute_due_follow_up()`.

## 13. RBAC

One new permission: `voiceagent.follow_up_actions:retry`, added to
`voiceagent.rbac_bootstrap.PERMISSIONS` and granted by
`bootstrap_tenant_rbac()` exactly like every other Phase 2.7 follow-up
permission. No `execute`/`claim` permission exists -- there is no route or
Tool Gateway tool that would ever check it, so registering one would
create a capability with no caller (brief §13). Background execution
(`FollowUpWorker`) runs against a `TenantContext` built directly per
tenant, still subject to Row-Level Security on every query it issues --
"system/background execution must still operate within tenant boundaries"
holds by construction, not by a permission check (there is no HTTP
request, hence no `core.rbac.can()` gate, to bypass).

## 14. Database

Migration `0007_extend_follow_up_execution`:

- Widens `ck_follow_up_actions_status` to the five-value vocabulary
  (drops and recreates; RLS/FORCE/grants on the table untouched).
- Adds `attempt_count` (`INTEGER NOT NULL DEFAULT 0`), `next_attempt_at`,
  `last_attempted_at`, `completed_at` (all `TIMESTAMPTZ NULL`),
  `failure_reason` (`VARCHAR(100) NULL`), `execution_id` (`UUID NULL`).
- Two new CHECK constraints: `attempt_count >= 0`; `failure_reason` in the
  closed vocabulary or `NULL`.
- One new partial index, `ix_follow_up_actions_claim_lookup` on
  `(tenant_id, next_attempt_at) WHERE next_attempt_at IS NOT NULL` -- the
  index `claim_due_follow_up()`'s query needs; partial because
  `next_attempt_at` is non-NULL only for `EXECUTABLE_TYPES` rows, a small
  minority of the table.

No existing RLS policy, grant, or constraint is weakened. One Alembic head
(`0007_follow_up_execution`, `down_revision = "0006_call_analysis"`).

## 15. Auditability

Every execution-lifecycle transition writes one `core.audit_log` entry via
`record()` (no second audit table): `follow_up.claimed` (claim),
`follow_up.completed`, `follow_up.failed` (attempt count, failure reason,
whether exhausted), `follow_up.reprocess_requested` (the administrative
retry, attributed `ActorType.USER`/`context.actor_id`), and
`follow_up.cancelled` -- but only when the cancelled follow-up's `type` is
in `EXECUTABLE_TYPES`: cancelling a `'contact'`/`'manual_follow_up'`
follow-up is unchanged from Phase 2.7 (no audit), since it never entered
the execution lifecycle. Claim/complete/fail are attributed
`ActorType.SYSTEM` (no human actor). Every audit `metadata` field is a
count, a type string, or a closed-vocabulary reason code -- never
conversation text, a calendar payload, or an exception message/trace
(brief §15/§17).

## 16. Testing

- **`tests/followups/test_retry_policy.py`** -- pure, hermetic: backoff
  formula values and monotonicity, `EXECUTABLE_TYPES`/`FAILURE_REASONS`
  contents, invalid `attempt_count` rejected.
- **`tests/followups/test_followup_lifecycle.py`** (extended) -- every
  legal/illegal transition in the five-status table, no-op self-transitions,
  terminal-status exhaustiveness.
- **`tests/followups/test_followup_models.py`** (extended) -- new columns,
  widened status CHECK, new CHECK constraints, new index.
- **`tests/followups/test_followup_worker.py`** -- hermetic, fake
  `DatabaseBoundary`/tenant list/execute function: bounded concurrency,
  per-tenant error isolation, idempotent `start()`, `shutdown()` cancels
  cleanly, a fixed number of claims per tick per tenant.
- **`tests/integration/test_follow_up_execution_integration.py`** (real
  PostgreSQL) -- lifecycle DB-touching paths, scheduling (future not due,
  due eligible, timezone-aware, no recurrence), concurrency (two real
  threads racing `claim_due_follow_up()` on the same row), idempotency
  (duplicate execution attempt, retry after simulated failure, no
  duplicate calendar event), stale/active lease recovery, tenant isolation
  (cross-tenant claim/execution denied, RLS enforced on every new column),
  bounded retries to a final `failed` state, the `GET`/`POST .../retry`
  routes end-to-end through the real Tool-Gateway-adjacent RBAC chain.

Full existing suite re-run for regressions (§20).

## 17. Non-Goals / Deferred

- Execution types beyond `'appointment'` (brief §5/§18) -- no generic
  HTTP/webhook/email/SMS/code-execution action.
- Recurring or campaign/bulk-scheduled follow-ups (brief §4/§18).
- A generic cron/scheduler API, workflow engine, or event bus (brief §18).
- A lease-renewal heartbeat for a long-running execution -- unnecessary at
  this phase's execution cost (one `get_event()` call), and would add a
  second liveness mechanism next to the lease itself.
- Redis as an authoritative execution-state store, or as anything at all in
  this phase's own mechanism -- PostgreSQL alone decides claim eligibility.
- Distributed leader election for the worker -- `SKIP LOCKED` already makes
  any number of concurrently running `FollowUpWorker` processes safe
  without one.
- Environment-variable-driven worker configuration
  (`voiceagent.config.settings`) -- `FollowUpWorker`'s constructor
  parameters are plain, operator-supplied values; wiring a
  `VOICEAGENT_FOLLOWUP_*` settings surface and a process entrypoint is left
  to the phase that actually stands up a deployed worker process.
- A `max_attempts_exceeded` failure reason is never written automatically
  in this phase (exhaustion is read from `attempt_count`/`MAX_ATTEMPTS`
  directly) -- reserved vocabulary for a future administrative "give up and
  record why" action, not currently exercised.
