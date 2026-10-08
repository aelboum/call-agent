# Phase 2.41 — Database Migration Rollback Validation

Closes the gap the Phase 2.36 production-readiness audit identified and
Phase 2.37 (`docs/PHASE-2.37-BACKUP-RECOVERY-FOUNDATION.md`) deliberately
left open: forward migrations were validated, but migration **rollback**
was documented behavior only, never exercised against a real database. This
phase runs the product's own 12-revision Alembic chain (`migrations/`)
through a disposable PostgreSQL instance in both directions, boundary by
boundary, with and without data; found exactly one real defect
(`0007_follow_up_execution`'s downgrade, §7); and remediates it with the
smallest possible fix, re-verified end to end against real PostgreSQL.

## 1. Migration topology

* **Script location**: `migrations` (`alembic.ini`'s `script_location`),
  versions under `migrations/versions/`.
* **Chain shape**: linear, single head, no branches, no `depends_on`
  cross-links. 12 revisions, base `0001_app_schema`, head
  `0012_inbound_routing`.
* **Full ordering** (base → head):

  | # | Revision | Summary |
  |---|---|---|
  | 1 | `0001_app_schema` | creates the `app` schema + default privileges, no tables |
  | 2 | `0002_domain_foundation` | `agents`, `agent_versions`, `phone_numbers`, `call_sessions` |
  | 3 | `0003_conversation_turns` | `conversation_turns` |
  | 4 | `0004_contacts_calendar` | `contacts`, `calendars`, `calendar_events`; `call_sessions.contact_id` |
  | 5 | `0005_call_outcomes_followups` | `call_outcomes`, `follow_up_actions` |
  | 6 | `0006_call_analysis` | `call_analysis` |
  | 7 | `0007_follow_up_execution` | extends `follow_up_actions` (execution metadata, widened status) |
  | 8 | `0008_call_workflow_executions` | `call_workflow_executions` |
  | 9 | `0009_knowledge_tables` | `knowledge_sources`, `knowledge_items` |
  | 10 | `0010_call_ai_analyses` | `call_ai_analyses` |
  | 11 | `0011_call_sessions_index` | composite `(tenant_id, status)` index only |
  | 12 | `0012_inbound_routing` | `inbound_call_routes`; unique `call_sessions.fs_channel_uuid` |

* No migration declares itself intentionally irreversible (no
  `NotImplementedError`/no-op `downgrade()` anywhere in the chain); every
  revision has a real `downgrade()`.
* Data migrations: none. Every migration is schema-only DDL (`op.execute`
  for grants/triggers/functions, `op.create_table`/`add_column`/etc.) —
  confirmed by reading all 12 files; no `op.execute("UPDATE ...")` or ORM
  bulk-write anywhere in `migrations/versions/`.
* `migrations/env.py` imposes one safety semantic beyond Alembic's own:
  `target_metadata = None`, permanently — autogenerate against SaaS-OS's
  shared `MetaData` is refused by construction, so nothing here can ever
  propose dropping/recreating a SaaS-OS table.
* SaaS-OS's own migration history (`infra.db.migrations`, run via
  `saas-os-migrate`) is a separate, independently-pinned chain
  (`alembic_version_saas_os`) applied first, per ADR-0016/ADR-0001. This
  phase touches only the product's own chain (`alembic_version`); SaaS-OS
  itself and the pinned commit `ff550010e5eafecace7311038aadc99fcecfbe3d`
  were not modified.

## 2. Infrastructure used

Disposable, throwaway `postgres:16-alpine` container (`phase241-rollback-pg`,
port 15432), the exact convention already established by
`tests/integration/README.md` and the CI `migrations-integration` job
(`.github/workflows/ci.yml`) — not a new harness, no `docker-compose.yml`
change, torn down at the end of this phase.

```bash
docker run -d --rm --name phase241-rollback-pg \
    -e POSTGRES_USER=saas_os -e POSTGRES_PASSWORD=devpassword \
    -e POSTGRES_DB=voiceagent -p 15432:5432 postgres:16-alpine

docker exec phase241-rollback-pg psql -U saas_os -d voiceagent -c "
    CREATE ROLE saas_os_app LOGIN PASSWORD 'devpassword'
        NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
    GRANT CONNECT ON DATABASE voiceagent TO saas_os_app;
"

export ENVIRONMENT=test
export DATABASE_URL="postgresql+psycopg://saas_os_app:devpassword@127.0.0.1:15432/voiceagent"
export MIGRATIONS_DATABASE_URL="postgresql+psycopg://saas_os:devpassword@127.0.0.1:15432/voiceagent"
export REDIS_URL="redis://127.0.0.1:1/0"
export APP_DB_USER=saas_os_app

saas-os-migrate upgrade   # SaaS-OS's own history, first (ADR-0016)
alembic upgrade head      # this product's history, second
```

## 3. Scenario A — clean forward migration (genuinely empty database)

A fresh container (no `app` schema, verified via `\dn` showing only
`public`) was migrated with the two commands above. Result: **clean**.
`alembic current`/`alembic heads` both report `0012_inbound_routing (head)`;
16 tables exist in `app`, all with zero rows. Row-Level Security inventory
(queried directly from `pg_class`) exactly matches
`tests/integration/test_domain_rls_integration.py::test_row_level_security_is_enabled_and_forced_for_every_table`'s
own pinned expectation — RLS **and** FORCE RLS enabled on all 15
tenant-scoped tables, both disabled only on `app.inbound_call_routes` (the
one documented pre-tenant-resolution exception). Composite tenant-safe
foreign keys spot-checked directly (e.g.
`fk_agent_versions_agent: FOREIGN KEY (agent_id, tenant_id) REFERENCES app.agents(id, tenant_id)`,
and the same shape on `phone_numbers`, `call_sessions`) — present as
expected on every table that has a tenant-scoped parent. The full
`pytest -m integration` suite (261 tests, every existing RLS/composite-FK/
immutability/lifecycle test) was then run against this same clean schema:
**261 passed, 0 failed**.

## 4. Scenario B/C — every downgrade boundary, both directions, full base round-trip

Implemented as four stepwise passes (not a blunt `downgrade base` +
`upgrade head` — every one of the 12 boundaries is exercised and verified
individually in both directions):

1. **head → base**, one revision at a time (12 steps). Every step's
   resulting `alembic current` matched its expected parent revision
   exactly.
2. **base → head**, one revision at a time (12 steps), capturing a full
   structural snapshot (table list, per-table column list with types/
   nullability, every constraint's `pg_get_constraintdef()`, RLS/FORCE
   flags, policies, triggers) after landing on each revision — this
   snapshot is the ground truth the rest of the validation compares
   against. Final state at head matched the Scenario A clean-forward
   snapshot exactly.
3. **head → base again**, one revision at a time, comparing the schema
   landed on at every step against pass 2's snapshot for that same
   revision.
4. **base → head again**, one revision at a time, same comparison, ending
   at head.

All four passes: **12/12 boundaries each, 48/48 individual migration steps
total, zero mismatches** — every `downgrade()`/`upgrade()` pair in this
chain is a structurally exact inverse of its sibling, on an empty database.
At base: `alembic_version` table exists but holds no row (`version_num` is
`NULL`); the `app` schema itself does not exist (`0001`'s `downgrade()`
runs `DROP SCHEMA IF EXISTS app RESTRICT`, which only succeeds because
every table was already dropped by the preceding steps — a `RESTRICT`,
not `CASCADE`, is deliberate: a future migration that forgets to drop its
own table would fail this step loudly instead of silently losing data).

### Per-revision rollback matrix

All 12 boundaries, tested both empty (passes 1–4 above) and — for the
specific boundary this phase found a real issue — with representative
data (§5). "Data impact" describes what downgrading **past** that revision
does to data that exists only because of it.

| Revision | Downgrade | Upgrade | Schema integrity | Data impact | Result |
|---|---|---|---|---|---|
| `0012_inbound_routing` | OK | OK | exact match | drops `inbound_call_routes`; unique constraint replaced by non-unique index (lossless for the index itself) | PASS |
| `0011_call_sessions_index` | OK | OK | exact match | drops one index only, no data | PASS |
| `0010_call_ai_analyses` | OK | OK | exact match | drops `call_ai_analyses` table and all rows | PASS (destructive by design — see §6) |
| `0009_knowledge_tables` | OK | OK | exact match | drops `knowledge_sources`/`knowledge_items` and all rows | PASS (destructive by design) |
| `0008_call_workflow_executions` | OK | OK | exact match | drops `call_workflow_executions` and all rows | PASS (destructive by design) |
| `0007_follow_up_execution` | OK — including with `follow_up_actions` rows in `'processing'`/`'failed'` (normalized to `'pending'` first; §7) | OK | exact match | normalizes `'processing'`/`'failed'` rows to `'pending'` before re-narrowing the status constraint; no other data touched | PASS (remediated — see §7) |
| `0006_call_analysis` | OK | OK | exact match | drops `call_analysis` and all rows | PASS (destructive by design) |
| `0005_call_outcomes_followups` | OK | OK | exact match | drops `call_outcomes`, `follow_up_actions` and all rows | PASS (destructive by design) |
| `0004_contacts_calendar` | OK | OK | exact match | drops `contacts`, `calendars`, `calendar_events`, and `call_sessions.contact_id` | PASS (destructive by design) |
| `0003_conversation_turns` | OK | OK | exact match | drops `conversation_turns` and all rows | PASS (destructive by design) |
| `0002_domain_foundation` | OK | OK | exact match | drops `agents`, `agent_versions`, `phone_numbers`, `call_sessions` and all rows | PASS (destructive by design) |
| `0001_app_schema` | OK | OK | exact match | drops the `app` schema itself (only possible once empty) | PASS |

## 5. Scenario D — data-bearing rollback/re-upgrade

Seeded, through the repository's own service layer (no second database
abstraction — `voiceagent.agents.service`, `voiceagent.calls.service`,
`voiceagent.contacts.service`, `voiceagent.calendars.service`,
`voiceagent.conversations.service`, `voiceagent.followups.service`,
`voiceagent.call_analysis.service`, `voiceagent.knowledge.service`,
`voiceagent.call_intelligence.service`, plus one direct ORM insert via
`tenant_session_scope` for `call_workflow_executions`, which has no
`create_*` service entry point for a *pre-finished* execution): two
tenants, each with an agent + published version, a phone number
(and its synced `inbound_call_routes` row), a contact, a `CallSession`
through its real `initiated → ringing → answered → completed` lifecycle,
two conversation turns, a `CallOutcome`, a calendar + event, a built
`CallAnalysis`, a knowledge source + item, a completed `CallAiAnalysis`
(via `request_analysis` → `claim_pending_analysis` → `complete_analysis`),
a finished `CallWorkflowExecution`, a plain `pending` follow-up, and —
specifically to probe `0007`'s widened status vocabulary — a second
follow-up driven through `claim_due_follow_up()` then
`fail_follow_up_execution()` to reach `status='failed'` with a non-null
`failure_reason` and `attempt_count > 0`, entirely through the
fully-supported application path.

Observed on downgrade, by category:

* **Lossless**: `0011` (index only), `0012`'s constraint-to-index swap.
* **Expected, documented data loss** (table-creation migrations):
  `0010`, `0009`, `0008`, `0006`, `0005`, `0004`, `0003`, `0002` each drop
  the table(s) they created — any data in them is gone, and re-upgrading
  recreates the table empty. This is correct, ordinary DDL-rollback
  semantics for a migration whose entire job was creating that table; it
  is not a defect, but is recorded here explicitly rather than silently
  assumed lossless (brief's own instruction).
* **Normalized, not lossy or blocked — the one defect, now remediated**:
  `0007_follow_up_execution`. See §7.

## 6. Full base round-trip with data present (post-remediation)

Starting from head with both tenants' data seeded (one follow-up left in
`'processing'`, one driven to `'failed'`), the chain was walked down one
boundary at a time (§4's matrix). Every boundary through `0008` succeeded
exactly as in the empty-database passes (data in the tables each step
drops is lost, as documented above). At the `0007 → 0006` boundary, the
remediated `downgrade()` first normalized both probe rows' `status` to
`'pending'` (verified directly against the database, not merely that the
step succeeded), then recreated the narrower historical constraint
successfully — confirmed both present and `convalidated` (actually
checked against every row, not added `NOT VALID`). The walk continued all
the way to `base` without any further issue, then back up to `head`
again. `agents`, `agent_versions`, `phone_numbers`, `contacts`,
`call_sessions`, `conversation_turns`, `call_outcomes`, `calendars`,
`calendar_events`, and `call_analysis` behaved exactly as in the
empty-database passes; `call_workflow_executions`, `knowledge_items`,
`knowledge_sources`, `call_ai_analyses`, and `follow_up_actions` itself
were emptied by their own table-drop boundaries exactly as in §5, then
recreated empty on the way back up — the two probe rows do not survive
the full base round-trip (nothing could: their owning table is dropped at
the `0005` boundary), which is expected and unrelated to the `0007` fix
itself; see §7 for the narrower, table-preserving round-trip that proves
normalization specifically.

## 7. The one defect — found, and remediated: `0007_follow_up_execution`'s downgrade

**File**: `migrations/versions/0007_extend_follow_up_execution.py`
**Revision**: `0007_follow_up_execution` (downgrade boundary `0007 → 0006_call_analysis`)
**Command**: `alembic downgrade -1` (from `0007_follow_up_execution`)

**Exact error**:

```
sqlalchemy.exc.IntegrityError: (psycopg.errors.CheckViolation) check constraint
"ck_follow_up_actions_status" of relation "follow_up_actions" is violated by some row
[SQL: ALTER TABLE app.follow_up_actions ADD CONSTRAINT ck_follow_up_actions_status
CHECK (status IN ('pending', 'completed', 'cancelled'))]
```

**Cause**: `0007`'s `upgrade()` widens `ck_follow_up_actions_status` from
three values to five (`'pending', 'processing', 'completed', 'cancelled',
'failed'`). Its `downgrade()` drops that constraint and recreates the
*original* three-value version unconditionally. `ALTER TABLE ... ADD
CONSTRAINT` validates every existing row against the new definition — so
any row a fully-supported application code path has already moved to
`'processing'` (`voiceagent.followups.service.claim_due_follow_up()`) or
`'failed'` (`fail_follow_up_execution()`) makes this `ALTER TABLE` fail.

**Database state immediately before failure**: at revision
`0007_follow_up_execution`, `app.follow_up_actions` containing at least one
row with `status = 'failed'`.

**Determinism**: confirmed reproducible — triggered on the first
multi-step `downgrade` attempt that crossed this boundary, and again on a
second, isolated single-step attempt (`alembic downgrade -1` from
`0007_follow_up_execution` directly) immediately afterward. Same error both
times.

**Is it destructive?** No — PostgreSQL DDL is transactional, and
`migrations/env.py` wraps the entire multi-revision downgrade plan in one
transaction (`context.begin_transaction()` around `context.run_migrations()`).
The failure rolled back cleanly every time: the database remained (or
returned to) exactly `0007_follow_up_execution`, and the probe row's
`status` was verified unchanged (`'failed'`) immediately after. This fails
loudly and safely; it does not corrupt data.

**Is it intentionally irreversible?** No — nothing in `0007`'s own
docstring, `docs/PHASE-2.9-STATUS.md`, or anywhere else in the repository
documents this as expected/accepted behavior. This is a genuine gap: the
migration's `downgrade()` was written to invert the *schema* change, not
to account for *data* the schema change itself makes representable.

**Classification**: real migration defect — a `downgrade()` that behaves
correctly only when `follow_up_actions.status` is already restricted to
the subset it is about to re-enforce, with no handling for the two new
values the forward migration introduced.

### 7.1 Root cause

`0007`'s `upgrade()` widens `ck_follow_up_actions_status` from
`('pending', 'completed', 'cancelled')` (the exact set `0005`'s
`upgrade()` originally established — `migrations/versions/0005_create_call_outcomes_followups_tables.py`,
`server_default='pending'`) to five values
(`'pending', 'processing', 'completed', 'cancelled', 'failed'`). No
migration after `0007` touches this column or constraint again — confirmed
by grepping `migrations/versions/0008_*.py` through `0012_*.py` for
`follow_up_actions`; `voiceagent.followups.models.FOLLOW_UP_STATUSES`
(the application's own current vocabulary) is exactly those same five
values, nothing more. `0007`'s `downgrade()` dropped the widened
constraint and recreated the original three-value one unconditionally,
with no handling for a row already in one of the two values it had itself
introduced.

### 7.2 Remediation

**File changed**: `migrations/versions/0007_extend_follow_up_execution.py`
(only this file — no other migration touched).

One statement added to `downgrade()`, immediately before the narrower
constraint is recreated:

```python
op.execute(
    "UPDATE app.follow_up_actions SET status = 'pending' WHERE status IN ('processing', 'failed')"
)
```

`upgrade()` is byte-for-byte unchanged. The recreated constraint's
definition is unchanged (`status IN ('pending', 'completed', 'cancelled')`
— still the exact original three values; nothing was broadened). No other
migration file was modified.

### 7.3 Why `'pending'` is the historically correct target, not an approximation

Both new values are, by the application's own lifecycle semantics
(`voiceagent/followups/service.py`), non-terminal:

* **`'processing'`** (`claim_due_follow_up()`) means a claim is currently
  in flight — not finished, not failed, not cancelled. Before `0007`
  existed there was no in-flight state at all; an unclaimed,
  not-yet-acted-on row was simply `'pending'`. `'processing'` is a
  *refinement* of what `'pending'` used to mean, not a new independent
  state — collapsing it back to `'pending'` on downgrade restores exactly
  the granularity that existed before this migration, losing only
  information the old schema had no way to represent in the first place.
* **`'failed'`** (`fail_follow_up_execution()`) is likewise not terminal
  in general: `voiceagent/followups/retry_policy.py`'s bounded-retry
  design means a `'failed'` row whose `attempt_count` has not yet reached
  `MAX_ATTEMPTS` is given a fresh `next_attempt_at` and is expected to be
  claimed again — it is a *transient* failure state, not a final outcome.
  The two genuinely terminal states this product's old three-value
  vocabulary already had are `'completed'` (success) and `'cancelled'`
  (explicit stop) — neither is correct for a row that has not succeeded
  and was not cancelled. `'pending'` — "not yet finished, eligible to be
  acted on again" — is the one old value whose meaning a
  non-exhausted-or-exhausted `'failed'` row (and an in-flight
  `'processing'` row) both still satisfy.
* No other value needs normalizing: `FOLLOW_UP_STATUSES` has exactly five
  members, and `0007` is the only migration that ever changes this
  constraint, confirmed in §7.1.

This was verified empirically, not just argued: the regression test below
seeds one row of each new value through the real application service
layer and asserts the exact post-downgrade status, not merely that the
migration no longer raises.

### 7.4 What did not change

* `upgrade()` — unchanged.
* The recreated historical constraint's allowed values — unchanged
  (still exactly `'pending', 'completed', 'cancelled'`); nothing was
  broadened to make the test pass.
* Current application status semantics
  (`voiceagent.followups.models.FOLLOW_UP_STATUSES`,
  `voiceagent/followups/service.py`) — untouched. The normalization runs
  only inside a `downgrade()` path that already drops the two newer
  columns/constraints this same migration added; it has no effect on a
  database running at or above `0007`.
* Every other migration (`0001`–`0006`, `0008`–`0012`) — untouched.

## 8. Scenario E — RLS and tenant isolation after rollback/re-upgrade

After the full §6 round-trip (head → base → head again, crossing the
remediated `0007` boundary with real data), the existing RLS/tenant-
isolation suite (`tests/integration/test_domain_rls_integration.py`, the
repository's own mechanism for this — no second one introduced) was
re-run in full against the recovered schema:

* 19/19 tests passed, including
  `test_row_level_security_is_enabled_and_forced_for_every_table` (RLS +
  FORCE RLS inventory, all 16 tables, unchanged from §3's clean baseline),
  `test_tenant_cannot_read_another_tenants_*` (four tables), cross-tenant
  composite-FK rejection (`test_agent_version_cannot_reference_another_tenants_agent`,
  `test_phone_number_cannot_reference_another_tenants_agent`,
  `test_call_session_cannot_reference_another_tenants_phone_number`), and
  the `AgentVersion`/`KnowledgeItem`/`CallAiAnalysis` immutability triggers.
* The dedicated remediation test itself (§9) additionally re-proves tenant
  isolation using its own two probe tenants (one left `'processing'`, one
  driven to `'failed'`) immediately after the downgrade-through-`0007`/
  upgrade-back-to-head round-trip: neither tenant's `tenant_session_scope()`
  can read the other's `Agent` row.
* The full `pytest -m integration` suite (all 264 tests, including the
  3 in `tests/ops/test_migration_rollback.py`) was re-run on a fresh
  disposable container after the remediation and passed in full (264/264,
  exit code 0), confirming nothing else regressed.

**Conclusion**: the rollback/re-upgrade round-trip — now including a
successful crossing of the `0007` boundary with real `'processing'`/
`'failed'` data — does not silently remove or weaken RLS, FORCE RLS,
policies, or tenant-safe composite foreign keys. Every property Scenario A
established still holds after the chain has been exercised backward and
forward.

## 9. Tests added

`tests/ops/test_migration_rollback.py` — real-PostgreSQL, marked
`pytest.mark.integration` (excluded from the default run, same convention
as `tests/integration/`), deterministic, no timing-dependent checks:

* `test_migration_chain_is_the_expected_linear_single_head_graph` — pins
  the exact 12-revision chain and single head; a future branch or
  silently-rewritten history fails this loudly.
* `test_full_downgrade_to_base_and_back_restores_clean_schema` — the §4
  walk, encoded as a regression test: head → base → head, asserting the
  rebuilt schema's structural inventory (tables, columns, constraint
  definitions) is identical to the inventory recorded before the walk
  started. Deliberately run against whatever `follow_up_actions` data
  other test files already left in the shared disposable database
  (including real `'processing'`/`'failed'` rows from
  `tests/integration/test_follow_up_execution_integration.py`) rather than
  pre-cleaned — this test passing with that real, un-sanitized leftover
  data is itself part of the end-to-end proof that the remediation works,
  not only in the dedicated probe below.
* `test_follow_up_action_status_downgrade_normalizes_processing_and_failed_rows`
  — the remediation's own dedicated proof. Seeds **two** tenants, each
  with a real follow-up reached through the application's service layer:
  one left in `'processing'` (`claim_due_follow_up()`, not failed), one
  driven to `'failed'` (`fail_follow_up_execution()`). Walks down through
  `0007` and asserts, in order: the downgrade **succeeds** (no
  `IntegrityError`); both rows are normalized to `'pending'` *in the
  database* (not merely that the step didn't crash); the recreated
  `ck_follow_up_actions_status` constraint is present, is the narrow
  three-value definition, and `pg_constraint.convalidated` is `true`
  (actually checked against every row, not added `NOT VALID`); the chain
  upgrades back through `0007` to head again, with the widened constraint
  restored and both rows still present (permanently `'pending'` — a
  downgrade's data effect is not undone by the matching upgrade, the same
  rule every other destructive boundary in this chain already follows);
  and, after that full round-trip, RLS/tenant isolation between the two
  probe tenants is unaffected (`tenant_session_scope()` cross-reads both
  ways return nothing). This explicitly covers both new values
  individually, not just one representative case, and would fail again
  if a future change ever weakened the normalization back to a bare
  "does it crash" check.

This module is intentionally schema-destructive (the second test drops
the entire `app` schema to `base` and rebuilds it) and is placed so it
collects after `tests/integration/` — see the module's own docstring. The
full `pytest -m integration` run including this module (264 tests: the
261 pre-existing integration tests plus these 3) passed in full on a fresh
disposable container, both before this remediation (to confirm the defect
reproduced) and after (to confirm the fix, with every other test
unaffected).

## 10. Limitations

* **No CI harness added** — same deliberate choice `tests/integration/README.md`
  already documents for this repository: a real PostgreSQL instance is
  required to run this module; it is not simulated, mocked, or run against
  SQLite. `pytest -m integration` against a real database is still a
  manual/CI-job action, not part of the default `pytest` run.
* **Single defect found, not a general audit of every possible data
  shape** — this phase seeded one representative data set per table, not
  an exhaustive fuzz of every column's boundary values. A different,
  not-yet-encountered data shape could in principle surface a different
  downgrade issue in a migration this phase marked PASS; §4's matrix
  reflects what was actually exercised.
* **The `0007` defect is fixed, not merely reported** — see §7.2;
  `migrations/versions/0007_extend_follow_up_execution.py` now carries
  the one-line normalization, re-verified against real PostgreSQL (§6,
  §9).
* **Windows-local validation, Linux CI not re-run** — this phase's
  commands were executed on the same Windows/Docker Desktop setup as prior
  phases' own manual validations (e.g. Phase 2.37); the CI
  `migrations-integration` job (Linux, GitHub Actions) was not re-triggered
  by this phase, though nothing in this phase changes that job's inputs.

## 11. Exact commands used

```bash
# Container + roles: see §2.

# Scenario A
alembic upgrade head
alembic current
alembic heads
pytest -m integration -q

# Scenario B/C (four-pass walk) and Scenario D/E: implemented as the
# tests in tests/ops/test_migration_rollback.py; run with:
pytest tests/ops/test_migration_rollback.py -m integration -v

# Scenario F
alembic branches
alembic heads
grep -h '^revision: str' migrations/versions/*.py | sort | uniq -c

# Full required verification
pytest -m integration -q
pytest -q
ruff check .
ruff format --check .
pyright
lint-imports
detect-secrets scan --baseline .secrets.baseline
```

## 12. Final verdict

**GREEN.**

Forward migration (upgrade path unchanged), every individual
downgrade/upgrade boundary on an empty database, the full base
round-trip, and RLS/tenant-isolation/composite-FK integrity after that
round-trip are all proven, against real disposable PostgreSQL, to work
exactly as the schema's own structure predicts. One real,
previously-undocumented defect was found (§7.1):
`0007_follow_up_execution`'s downgrade failed — safely, loudly, and
deterministically, never destructively — when a `follow_up_actions` row
already carried one of the two status values that migration itself
introduced. It has been remediated with the smallest possible fix (§7.2:
one `UPDATE` statement, scoped entirely to `0007`'s own `downgrade()`,
`upgrade()` and every other migration untouched), the historical
correctness of the chosen target value is argued and empirically verified
(§7.3), and the fix is re-proven end to end against real PostgreSQL: the
same boundary that previously failed now succeeds with real
`'processing'`/`'failed'` data, normalizes it correctly, upgrades back to
head, and leaves RLS/tenant isolation intact (§6, §8, §9). The full
264-test `pytest -m integration` suite and the full default suite both
pass after the fix, with no other file or migration changed.
