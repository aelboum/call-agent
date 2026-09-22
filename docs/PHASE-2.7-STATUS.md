# Phase 2.7 Status: Call Outcomes & Follow-up Primitives

## 1. Objective

Give the platform a small, durable, call-centric way to know what happened
on a call and what follow-up, if any, resulted from it: a business
`CallOutcome` distinct from `CallSession.status`'s technical lifecycle, a
deliberately small `FollowUpAction` (never a generic task), an appointment
follow-up that reuses Phase 2.6's Calendar service rather than duplicating
it, and two new Tool Gateway tools so an AI agent can record both from
inside a call. Not a workflow engine, not a CRM, not task management (see
§17 Non-Goals).

## 2. Architecture

```text
CallSession
    |
    +-- ConversationTurn[]           (Phase 2.5)
    +-- Contact (optional)           (Phase 2.6)
    +-- CallOutcome (optional, one)  (Phase 2.7)
    +-- FollowUpAction[] (optional)  (Phase 2.7)
            |
            +-- CalendarEvent (optional, only for type="appointment")
```

`FollowUpAction.call_session_id` references the call directly (not through
`CallOutcome`) -- the brief's own §7 field list has no `call_outcome_id`,
only `call_session_id`; the ASCII diagram in the brief's §3 is the
conceptual "why", not the literal FK graph. One new package,
`voiceagent.followups`, owns both aggregates -- exactly the shape
`voiceagent.calendars` already established for two related tables in one
package with one service, and the brief's own §10 frames "the Follow-up
service" as responsible for both `create_call_outcome`/`get_call_outcome`/
`update_call_outcome` and the four follow-up operations.

`voiceagent.followups.service` coordinates with `voiceagent.calendars
.service` for the one case that needs it (an appointment follow-up) and
never duplicates its timezone/overlap logic -- see §7.

## 3. CallOutcome Model

`voiceagent/followups/models.py::CallOutcome`.

```text
id, tenant_id, call_session_id, contact_id (nullable), outcome, notes
(nullable), created_at, updated_at
UNIQUE(call_session_id)   -- at most one current outcome per call (brief §6)
FK (call_session_id, tenant_id) -> call_sessions(id, tenant_id)
FK (contact_id, tenant_id)      -> contacts(id, tenant_id)
CHECK outcome IN ('resolved', 'appointment_scheduled', 'follow_up_required',
                   'no_answer', 'wrong_number', 'not_interested')
```

**`outcome` vocabulary deliberately differs from the brief's own literal
example list** -- see §14 Deviations for why "completed" became "resolved".
`CallOutcome.outcome` (the business result) and `CallSession.status` (the
technical lifecycle) are disjoint vocabularies by construction, verified
directly: `tests/followups/test_followup_models.py
::test_call_outcomes_outcome_check_does_not_collide_with_call_session_status`
asserts `OUTCOME_VALUES.isdisjoint(VALID_STATUSES)`.

A changed business result updates this one row; this phase keeps no
outcome history or event-sourcing (brief §6).

## 4. FollowUpAction Model

`voiceagent/followups/models.py::FollowUpAction`.

```text
id, tenant_id, call_session_id, contact_id (nullable), type, status,
due_at (nullable), calendar_event_id (nullable), description (nullable),
created_at, updated_at
FK (call_session_id, tenant_id)   -> call_sessions(id, tenant_id)
FK (contact_id, tenant_id)        -> contacts(id, tenant_id)
FK (calendar_event_id, tenant_id) -> calendar_events(id, tenant_id)
CHECK type   IN ('appointment', 'contact', 'manual_follow_up')
CHECK status IN ('pending', 'completed', 'cancelled')
CHECK (type = 'appointment') = (calendar_event_id IS NOT NULL)
```

No category, priority, assignee, project id, label, workflow id, automation
id, recurrence, dependency, or subtask field exists here or ever will in
this package (brief §7's own explicit list) -- verified directly:
`tests/followups/test_followup_models.py::test_no_hard_delete_api_exists`
also confirms no service function deletes a row (cancellation is a status
transition, never a hard delete -- brief §16).

## 5. Relationships

- `CallOutcome.contact_id` / `FollowUpAction.contact_id`: when a caller does
  not supply one explicitly, it is auto-derived by reading the call's own
  `CallSession.contact_id` -- **read**, never accepted as a Tool Gateway
  argument (ADR-0003 point 4; see §8). An explicit override is honored only
  from a non-tool caller (the REST API) and validated exactly like
  `voiceagent.calendars.service.create_event()`'s own optional `contact_id`.
- `FollowUpAction.calendar_event_id` is set **if and only if**
  `type == 'appointment'` -- enforced both by the database `CHECK` above
  and by `create_follow_up()`'s own application-level check (this
  product's established "CHECK plus application validation, never one
  alone" discipline).

## 6. State Transitions

`CallOutcome`: no state machine -- `create` (fails if one exists),
`update`/`set` (fails or succeeds accordingly). `set_call_outcome()` is the
one idempotent create-or-update operation (brief §12: "remains idempotent
where practical"), used by both the `PUT` route and the `call.set_outcome`
tool.

`FollowUpAction` (`voiceagent/followups/lifecycle.py`, pure and DB-free,
mirroring `voiceagent.calls.lifecycle`):

```text
pending -> completed | cancelled
completed | cancelled -> (terminal; no further transition)
```

A redelivered transition to the *current* status (including a terminal
one) is a no-op, not an error -- the same "duplicate events must not
corrupt state" rule `voiceagent.calls.lifecycle` already establishes.

## 7. Calendar Integration

`voiceagent.calendars.service` is never duplicated. For a `type="appointment"`
follow-up, `create_follow_up()`:

- validates an existing `calendar_event_id` for tenant ownership, if given; or
- calls `calendar_service.create_event()` when `calendar_id`/`start_at`/
  `end_at` are all given, capturing the resulting event's id; or
- **raises `FollowUpAppointmentRequiresCalendarEventError`** if neither --
  the brief's §8 asks for exactly one deterministic rule ("reject, or an
  explicit documented pending state"); this phase chose reject, since a
  half-created follow-up with no calendar event and no path to get one
  would be a worse "pending" than an outright, immediately-actionable
  error.

Ownership, timezone validation, overlap checking, and cancellation all stay
inside `voiceagent.calendars.service` exactly as Phase 2.6 built them --
verified against real PostgreSQL in
`tests/integration/test_call_outcomes_followups_integration.py
::test_appointment_follow_up_with_overlapping_interval_is_rejected`.

## 8. API

```text
GET  /v1/call-sessions/{id}/outcome
PUT  /v1/call-sessions/{id}/outcome

GET  /v1/call-sessions/{id}/follow-ups
POST /v1/call-sessions/{id}/follow-ups

GET  /v1/follow-ups/{id}
POST /v1/follow-ups/{id}/complete
POST /v1/follow-ups/{id}/cancel
```

Outcome and call-scoped follow-up routes live on the existing
`voiceagent.api.v1.call_sessions` router (matching Phase 2.6's own
precedent of adding the `/contact` association route there rather than a
new file); the id-scoped follow-up routes live in a new
`voiceagent.api.v1.follow_ups`, mirroring `calendar_events.py`'s split from
`calendars.py`. No `DELETE` anywhere (brief §16). Domain validation
failures map to `422`; an already-existing outcome or an illegal follow-up
transition maps to `409` via the existing `api.errors.conflict()` helper;
not-found maps to `404` -- no new error shape was invented.

## 9. Tool Gateway

Two new `ToolDefinition`s registered into the existing, single
`TOOL_REGISTRY` (no second tool execution path):

```text
call.set_outcome       -> voiceagent.followups.service.set_call_outcome
call.create_follow_up  -> voiceagent.followups.service.create_follow_up
```

Both follow the identical `Tool -> application service -> DatabaseBoundary`
shape Phase 2.6 established: `ctx.db.run(...)`, never `voiceagent.db`/
`voiceagent.tenancy.tenant_scope` directly (`tests/architecture
/test_tool_gateway_isolation.py
::test_no_tool_module_imports_voiceagent_db_directly`, unmodified and still
passing). Every domain error is normalized to a fixed `ToolExecutionError`
code -- never a raw exception, never tenant-sensitive content in the
message.

**Neither input model has a `call_id`/`call_session_id` field** (a
deliberate deviation from the brief's literal §11 input list -- see §14).
**Neither input model has a `contact_id` field** either (§5). No new field
was added to `ToolExecutionContext`: Phase 2.6 already added
`tenant_context`/`db`, and that is all these two handlers need.

## 10. RBAC

```text
voiceagent.call_outcomes       read, create, update
voiceagent.follow_up_actions   read, create, complete, cancel
voiceagent.tools               + call.set_outcome, call.create_follow_up
                                (one permission per tool, matching the
                                Phase 2.4/2.6 pattern exactly)
```

Two resources in one package, matching `voiceagent.calendars.permissions`'s
own two-resources-one-package shape. Registered via
`voiceagent.followups.permissions::register()` (never called at
import/build time) and wired into `voiceagent.rbac_bootstrap.PERMISSIONS`/
`register_permissions()`.

## 11. RLS

Both new tables: RLS `ENABLE` + `FORCE`, verified against real PostgreSQL
(`tests/integration/test_domain_rls_integration.py
::test_row_level_security_is_enabled_and_forced_for_every_table`, extended
this phase to list all ten tenant-owned tables). Every FK into a Phase 2.7
table is the established composite tenant-aware pattern.

## 12. Audit / Privacy

No outcome note, follow-up description, contact phone/email, or appointment
detail is logged anywhere in the new code. The Tool Gateway's existing
audit call is unchanged -- its `metadata` still carries only `tool_id`/
`call_session_id`/`status`, never tool arguments or results
(`tests/tools/test_gateway.py::test_audit_metadata_never_carries_tool_arguments`,
unmodified and still passing). Every new domain error class carries a
fixed, static message -- none of them echoes an outcome value, note,
description, or id back into an exception string beyond the identifiers
already safe to log elsewhere in this codebase (e.g. `call_session_id` in
`CallOutcomeNotFoundError`, matching `CallSessionNotFoundError`'s own
existing precedent).

## 13. Tests

**Hermetic** (`pytest`, no PostgreSQL): 479 passed (baseline 430 + 49 new:
`tests/followups/` (28: models, lifecycle, pure service-validation paths),
`tests/tools/test_handlers.py` additions (15, both new tools), plus one
pre-existing test renamed/updated for the new tool count (`test_registry.py`
-- see §14).

**Integration** (`pytest -m integration`, real PostgreSQL 16): 117 passed
(baseline 92 + 25 new,
`tests/integration/test_call_outcomes_followups_integration.py`, covering
exactly the §22-brief list: tenant isolation for both tables, cross-tenant
call/outcome and call/follow-up FK rejection, cross-tenant contact
rejection, the one-outcome-per-call invariant, real Tool Gateway
authorization (allowed, denied-by-allowlist, denied-by-missing-RBAC-
bootstrap), appointment-follow-up calendar-event creation, overlap
rejection, an existing-event path, cancelled-follow-up durability, and RLS
under the restricted `saas_os_app` role), plus one pre-existing test
updated for the two new tables (§14).

**Total: 596 passed, 0 failed.** No existing test was removed or weakened;
every changed pre-existing test is documented in §14 with why.

## 14. Quality Gates

All run and green in this environment:

- `ruff check .` -- clean.
- `ruff format --check .` -- clean.
- `pyright` -- 0 errors, 0 warnings.
- `lint-imports` -- 8 contracts kept, 0 broken.
- `detect-secrets scan` on every file this phase added or modified -- 0
  findings (the one pre-existing false positive at `tests/test_migrations.py`
  line 25, an unchanged dummy `postgresql+psycopg://owner:unused@...` URL,
  predates this phase and was not touched; `.secrets.baseline` was left
  exactly as it was at the start of this phase).
- `pytest` (hermetic) and `pytest -m integration` (real PostgreSQL) -- both
  green, see §13.
- No frontend files were touched; the frontend check was not run.

## 15. Deviations

- **Outcome vocabulary changed from the brief's literal example list.**
  Brief §5 gives `completed` as an example `CallOutcome.outcome` value while
  simultaneously requiring "choose names that do not conflict with
  `CallSession.status`" -- but `CallSession.status` (Phase 2.1) already
  includes `'completed'`. This phase kept the brief's collision-avoidance
  *rule* over its literal *example*: `resolved` replaces `completed`; the
  other five example values (`appointment_scheduled`, `follow_up_required`,
  `no_answer`, `wrong_number`, `not_interested`) are unchanged and already
  disjoint from `CallSession.status`.
- **Neither tool input model carries `call_id`/`contact_id`**, despite the
  brief's own §11 listing `call_id` as an input field for both tools. Every
  existing in-call tool (`HangupInput`, `TransferInput`, the four Phase 2.6
  tools) already establishes that a tool acts on *this* call only, taken
  from `ToolExecutionContext.call_session_id`, never from model output --
  ADR-0003 point 4 states this as a platform invariant ("identity and scope
  are never taken from model output... what would be difficult to change
  later: once a single tool accepts an identifier from model output, ...
  the invariant cannot be restored by review"). Per this task's own §4
  ("do not guess existing conventions when repository evidence exists"),
  the established pattern overrides the brief's literal field list; both
  tools instead read `ctx.call_session_id` and auto-derive `contact_id`
  from the call's own association (§5).
- **`voiceagent.followups` owns both `CallOutcome` and `FollowUpAction`** in
  one package with one service module, rather than two packages -- matching
  `voiceagent.calendars`'s own precedent and the brief's own §10 framing of
  a single "Follow-up service" responsible for both.
- **Appointment-without-calendar-event is a hard rejection**, not a pending
  state (§7 above) -- the brief's §8 offered either as acceptable; this
  phase picked the one that never leaves a follow-up row in an ambiguous
  state.
- **`FollowUpAction.call_session_id` references the call directly**, not
  through `CallOutcome` -- the brief's own §7 field list has no
  `call_outcome_id`; the §3 diagram's nesting is read as conceptual, not a
  literal FK requirement (see §2 above).

## 16. Limitations

- `GET /v1/call-sessions/{id}/follow-ups` returns every follow-up for that
  call unpaginated -- acceptable at the expected scale (a handful of
  follow-ups per call) but would need pagination if that assumption
  changes.
- The appointment follow-up's calendar-event title is a fixed, generated
  string (`"Follow-up for call {call_session_id}"`) -- the brief's tool
  input for `call.create_follow_up` has no separate title field for the
  underlying calendar event, only `description` for the follow-up itself.
- No rescheduling of an appointment follow-up (mirrors
  `voiceagent.calendars.service`'s own Phase 2.6 limitation: no
  `reschedule_event()` exists to call).

## 17. Explicit Non-Goals (confirmed absent)

CRM, lead management, sales pipelines, campaigns, marketing automation,
general task management, a generic workflow engine or workflow builder, an
event bus, a background automation engine, recurring follow-ups, task
assignment, task priorities, subtasks, reminders, email, SMS, automatic AI
disposition, AI summarization, embeddings, a vector database, sentiment
analysis, any external calendar provider (Google Calendar, Microsoft
Graph, CalDAV), a realtime engine change, a new AI provider, billing,
entitlements, a frontend redesign, and no change to SaaS-OS, previous
migrations, or the existing `CallSession`/`CalendarEvent` lifecycles.

## 18. Phase 2.8 Readiness

The Call Outcome / Follow-up foundation is fully reachable from an AI agent
through the existing Tool Gateway, gated by the existing AgentVersion
allowlist and RBAC, with real-PostgreSQL-verified tenant isolation and a
verified appointment-follow-up path into the existing Calendar service. A
future phase adding automatic AI disposition or summarization can read
`CallOutcome`/`FollowUpAction` through the existing service layer without
any schema change here; a future phase adding reminders/notifications can
poll `FollowUpAction.due_at` the same way, again without touching this
phase's schema or RBAC shape.
