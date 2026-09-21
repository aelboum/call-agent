# Phase 2.1 — Core Domain Foundation: STATUS

**Status**: IN PROGRESS (implementation complete, awaiting review; not
committed)
**Updated**: 2026-09-21
**SaaS-OS pin**: `ff550010e5eafecace7311038aadc99fcecfbe3d` (unchanged, verified)

Phase 2.0 (`docs/PHASE-2.0-ARCHITECTURE.md`) is authoritative for the
architecture; this document records what was actually built against it and
one correction discovered during implementation (§9 below).

---

## 1. Objective

Implement the minimum durable domain foundation Phase 2.0 §23 specified:
`Agent`, `AgentVersion`, `PhoneNumber`, `CallSession` — tenant-safe
persistence, immutable `AgentVersion` semantics, lifecycle state machines,
composite tenant-aware foreign keys, Row-Level Security, a repository/service
boundary, the minimal API contract, deterministic migrations, and security
tests that prove the properties rather than merely assert intent.

## 2. Implemented tables

Exactly the four Phase 2.0 §23 specifies, in schema `app`, no more:
`agents`, `agent_versions`, `phone_numbers`, `call_sessions`. No
`conversations`, `contacts`, `calendars`, `tools`, `workflows`, `recordings`,
`provider_credentials`, or `runtime_assignments` — verified both by a
hermetic test (`tests/test_migrations.py::
test_deferred_domain_tables_are_never_created`) and by direct inspection of a
real, migrated database (`\dt app.*` listed exactly these four).

## 3. Implemented invariants

- **Tenant isolation** — `tenant_id` on every table, RLS `ENABLE` + `FORCE`,
  policy keyed on `current_setting('app.tenant_id', true)`.
- **Composite tenant-aware foreign keys** on every parent/child relationship:
  `agent_versions → agents`, `agents → agent_versions` (the two draft/
  published pointers), `phone_numbers → agents`, `phone_numbers →
  agent_versions`, `call_sessions → phone_numbers/agents/agent_versions` — 8
  composite FKs total, each requiring both the id and the tenant_id to match.
- **Global E.164 uniqueness** on `phone_numbers.e164` (not
  `(tenant_id, e164)`), with the required generic-conflict handling in
  `voiceagent.phone_numbers.service.register_phone_number()`.
- **AgentVersion immutability** — a database trigger rejects any mutation of
  a `published` row except the single `published → archived` transition.
- **`agent_versions.published_by` carries no foreign key** into
  `core.users` — a plain value, per Phase 2.0 §11.5.
- **No runtime-only state persisted** — the live `EngineSession`, in-flight
  audio, and unfinished transcript never touch `call_sessions` or any other
  table (there is nothing to touch yet: no engine exists).

## 4. API surface

Exactly the endpoints in Phase 2.0 §23.9, no more:

```
POST   /v1/agents
GET    /v1/agents/{agent_id}
GET    /v1/agents
PATCH  /v1/agents/{agent_id}
POST   /v1/agents/{agent_id}/versions
POST   /v1/agents/{agent_id}/versions/{version_id}/publish
POST   /v1/agents/{agent_id}/versions/{version_id}/archive
POST   /v1/phone-numbers
GET    /v1/phone-numbers
PATCH  /v1/phone-numbers/{phone_number_id}
GET    /v1/call-sessions/{call_session_id}
GET    /v1/call-sessions
```

No `GET .../versions` list route, no `POST`/`PATCH` on `call-sessions`, no
`DELETE` anywhere — none of these are in Phase 2.0's contract. (A
`GET .../versions` route was drafted during implementation and removed before
this report, once cross-checked against §23.9 — see §9.)

## 5. Migration

`migrations/versions/0002_create_agent_phone_call_tables.py`
(`revision: 0002_domain_foundation`, `down_revision: 0001_app_schema`).
Creates all four tables, their `CHECK`/`UNIQUE`/composite-FK constraints, ten
indexes, RLS (`ENABLE`+`FORCE`+policy) on all four, explicit `GRANT`s to the
application role, and the `agent_versions_immutable` trigger. `alembic`
imports SQLAlchemy directly (ADR-0007's sanctioned migration layer);
`target_metadata` remains `None`; no `create_all()` path exists anywhere.

## 6. RLS / security verification

Verified against a real, throwaway PostgreSQL 16 instance (not a repository
dependency — see `tests/integration/README.md`), connected as the actual
restricted `saas_os_app` role (`NOSUPERUSER NOBYPASSRLS`), not a superuser:

- `pg_class.relrowsecurity`/`relforcerowsecurity` are both `true` for all
  four tables.
- Tenant A cannot read tenant B's agent, agent version, phone number, or call
  session (`session.get()` returns `None` in every case).
- A session that never sets `app.tenant_id` reads **zero** rows, not every
  tenant's rows — confirming the policy's deny-by-default behavior
  (`NULLIF(current_setting(...), '')::uuid` → `NULL`, and `tenant_id = NULL`
  is never true).
- Three composite-FK isolation attempts (an `agent_versions`, a
  `phone_numbers`, and a `call_sessions` row each referencing a real parent
  row that belongs to a *different* tenant than the child's own `tenant_id`)
  each raised `IntegrityError` — rejected at the constraint layer, not merely
  hidden by RLS.

## 7. AgentVersion immutability verification

- A direct `UPDATE` of `config` on a `published` row raised (trigger
  exception containing `"immutable"`).
- A direct `UPDATE` of `config_hash` on a `published` row raised identically.
- The one permitted transition (`published → archived`) succeeded and left
  `config`/`config_hash` byte-for-byte unchanged.
- Publishing an already-`published` version raised
  `AgentVersionNotDraftError` (application-layer guard, ahead of the
  database trigger).
- Changing an `Agent`'s mutable fields (`update_agent(..., name=...)`) left
  an already-published `AgentVersion`'s `config`/`config_hash`/`status`
  completely unchanged (historical version stability).

## 8. CallSession lifecycle verification

States: `initiated → ringing → answered → in_progress →
{completed | failed | interrupted}` (terminal). Verified: the full
happy-path sequence; a redelivered terminal event is a no-op (`ended_at`
unchanged on the second delivery); an illegal transition out of a terminal
state raises `InvalidCallSessionTransitionError`; every transition and
no-op/illegal case is also covered hermetically at the pure state-table level
(`tests/calls/test_lifecycle.py`, 18 parametrized cases) with no database
involved.

## 9. Deviations from Phase 2.0

One correction, and one endpoint self-caught before it shipped:

1. **The `agent_versions_immutable` trigger's exact SQL, as given in Phase
   2.0 report §23.6, would have failed at runtime.** It compared
   `NEW.config = OLD.config` directly; PostgreSQL's `json` type (unlike
   `jsonb`) has no equality operator — `json = json` raises `operator does
   not exist: json = json`. Every `published → archived` transition would
   have errored, not merely been rejected. The migration casts both sides to
   `text` for that one comparison instead (`NEW.config::text =
   OLD.config::text`); the archive-transition test above proves the fix
   works, and the config-mutation-rejection tests prove the trigger still
   correctly rejects real mutations. This is a SQL-correctness fix within an
   already-approved design, not a change to any decision, boundary, or
   requirement — no architecture was reopened.
2. **A `GET /v1/agents/{id}/versions` route was drafted, then removed**
   before this report, on re-checking it against Phase 2.0 §23.9's exact
   endpoint list (which specifies only `POST .../versions`,
   `.../publish`, `.../archive` — no list route). Caught during
   implementation, not after.
3. **Timestamp set-points for `call_sessions.started_at`/`answered_at`/
   `ended_at`/`duration_ms`** are an implementation clarification, not a
   contradiction: Phase 2.0 §23.5 names these columns without fixing their
   exact set-points. This phase adopts: `started_at` at creation,
   `answered_at` on first entry into `answered`/`in_progress`, `ended_at` on
   entry into any terminal state, `duration_ms` as `ended_at − started_at`.
   Documented in `voiceagent/calls/service.py`'s module docstring so a
   future phase does not have to reverse-engineer it from behavior.

No other deviation. Every table name, column name, physical type, default,
nullability, constraint, index, and the RLS/trigger mechanism match Phase
2.0 §23 exactly.

## 10. Known limitations

- **RBAC permissions are declared but not granted to any role.**
  `voiceagent.agents.permissions.register()` (and the `phone_numbers`/`calls`
  equivalents) call `core.rbac.register_permission()` — deliberately never
  at import or app-build time (would violate the Phase 1 no-DB-on-import
  invariant) — but nothing in this phase calls `register()`, and nothing
  grants the resulting permissions to a role. A route exercised through the
  real HTTP/RBAC chain will currently deny every caller until an operator (or
  a future setup script) performs that one-time bootstrap. This is a real,
  scoped gap, not an oversight: building that bootstrap tooling is not part
  of the four-table domain foundation this phase was asked to deliver.
- **No end-to-end HTTP-level test exercises the real RBAC chain** for the
  same reason (it needs the grant above, plus a real session/JWT, which is
  its own setup). The API layer's structure, error mapping, and the fact that
  it authorizes through `require_tenant()` are verified directly; the
  security-critical *data* properties (RLS, composite FKs, immutability,
  generic-conflict handling, lifecycle) are verified against a real database
  at the service layer, which is where those properties actually live.
- **No local/CI-permanent PostgreSQL harness exists** (Phase 2.1 brief §25
  asked that this be documented rather than silently introduced). See
  `tests/integration/README.md`.

## 11. Verification results

| Check | Result |
|---|---|
| `pytest` (hermetic, default) | **136 passed**, 19 deselected (`-m integration`) |
| `pytest -m integration` (real PostgreSQL) | **19 passed** |
| `ruff check` | Clean |
| `ruff format --check` | Clean |
| `pyright` | **0 errors** |
| `lint-imports` | **3 contracts kept, 0 broken** |
| `detect-secrets` | 0 real findings (a scan-run mutation to `.secrets.baseline`, from placeholder connection strings/passwords in test fixtures and CI config, was reverted — not a real finding) |
| Frontend `tsc --noEmit` / `vite build` | Clean |
| Offline migration SQL (`alembic upgrade head --sql`) | Renders correctly, single head |
| **Live migration apply** (real PostgreSQL, both SaaS-OS's and this product's history) | Succeeded |
| **Live migration `downgrade base` then re-`upgrade head`** | Succeeded; schema fully removed and cleanly recreated |
| SaaS-OS pin resolved | `ff550010e5eafecace7311038aadc99fcecfbe3d` (`direct_url.json`, re-verified) |
| SaaS-OS working tree | Clean, unmodified |

## 12. Scope-creep check

Not implemented, confirmed by direct inspection of the diff: any STT/LLM/TTS/
realtime provider, Pipecat, any `ConversationEngine` implementation, any
FreeSWITCH/ESL/`mod_audio_stream` code, the Tool Gateway or any tool, any
Contact/Calendar table or code, `conversations`/recordings/
`provider_credentials`/`runtime_assignments` tables, Docker/Compose, or
frontend feature work. `voiceagent/runtime/` and
`voiceagent/telephony/freeswitch/` remain empty, exactly as Phase 1 left
them.

## 13. Phase 2.2 readiness

Ready to begin once this foundation is reviewed. Phase 2.2's own entry
criteria (Phase 2.0 report §5's decisions on OD-3/OD-5, and the first
domain migration now existing) are satisfied; the RBAC-grant bootstrap noted
in §10 above should be resolved before any route is exercised by a real
tenant caller, but does not block starting the FreeSWITCH/engine work Phase
2.2 is scoped for.
