# ADR-0007: Persistence boundary — `infra.db` for models, full SQLAlchemy for migrations

Status: Accepted (Phase 0.1)
Date: 2026-09-21
Resolves: Phase 0 report §19 OD-4

## Context

Phase 0 recorded a gap (G-4): `infra.db` re-exports a narrow set of ORM
primitives, and several types the product expected — `JSONB`, `ARRAY`, `Enum`,
`Time`, `Float`, `BigInteger`, `LargeBinary` — are absent. The open question was
whether to live within that surface, or to import SQLAlchemy and PostgreSQL
types directly in product models.

Direct inspection of the pinned dependency
(`ff550010e5eafecace7311038aadc99fcecfbe3d`) changed the shape of this question
entirely. **The omissions are not oversights. They are security decisions with
recorded live-audit evidence.** `infra/db/orm.py`'s own module docstring
documents two of them:

- `sqlalchemy.func` is deliberately never re-exported (finding CP-07
  J-INFRA-05): `func.<anything>` compiles to a call to *any* PostgreSQL
  function, and an audit proved that ordinary Core/Product code holding
  `select` + `func` can execute `func.set_config('app.tenant_id', ...)` inside a
  tenant-scoped session, **overwriting the session's tenant and bypassing
  Row-Level Security for the rest of that transaction** — and, because
  `set_config`'s third argument controls transaction- vs session-scoping,
  potentially for whatever the pooled connection is reused for next.
- `sqlalchemy.text` is not re-exported either (privacy re-audit RA-01): it
  reopens the identical capability class —
  `session.execute(text("SELECT set_config('app.tenant_id', :t, false)"))`
  reads another tenant's rows and poisons the pooled connection past `COMMIT`.

Only `now()` and `sum_()` are exported in place of `func`: named,
single-purpose, and incapable of expressing a call to anything else in the
PostgreSQL function catalogue.

This reframes the decision. Importing SQLAlchemy directly in product model or
service code would not merely be a stylistic deviation from the platform's
conventions — **it would restore, inside this product, the exact RLS-bypass
capability class SaaS-OS removed on the strength of a live audit.** Tenant
isolation is this platform's foundational invariant (SaaS-OS ADR-0002), and call
recordings and transcripts are among the most sensitive data it will hold.

Two further facts from the same inspection:

- SaaS-OS's `"Only infra/db may import SQLAlchemy or psycopg directly"`
  import-linter contract lists `source_modules = ["core", "products",
  "control_plane"]`. Those are SaaS-OS's *own* packages. **The contract does not
  mechanically bind an external consumer repository.** The discipline is
  therefore ours to impose on ourselves, and the reason to impose it is the
  security argument above, not deference to convention.
- The platform's own sanctioned consumer fixture,
  `examples/reference-consumer/`, draws the line in exactly one place: its
  *model* (`models.py`) imports only from `infra.db` and never `sqlalchemy`,
  while its *migration* imports `sqlalchemy as sa` freely and uses `sa.Uuid()`,
  `sa.func.now()` and raw `op.execute()` DDL.

## Verified primitive inventory (at the pinned SHA)

Exported by `infra.db` and usable directly:

`Base` (a `DeclarativeBase`), `UUIDPrimaryKeyMixin`, `TimestampMixin`,
`Mapped`, `mapped_column`, `String`, `Text`, `Integer`, `Numeric`, `Boolean`,
`DateTime`, `ForeignKey`, `ForeignKeyConstraint`, `UniqueConstraint`,
`CheckConstraint`, `Index`, `JSON`, `now()`, `sum_()`, `select`, `update`,
`delete`, `IntegrityError`, `OperationalError`, plus the session/engine surface
(`session_scope`, `tenant_session_scope`, `acquire_tenant_advisory_lock`,
`get_engine`, `build_engine`, `build_session_factory`, `get_session_factory`,
`Session`), the RLS helper `tenant_rls_statements`, the role guard
(`validate_application_role`, `ApplicationRoleValidation`,
`UnsafeDatabaseRoleError`), config accessors and `run_core_migrations`.

Not exported, and the reason:

| Absent | Reason | Product answer |
|---|---|---|
| `func`, `text` | **Security** — RLS bypass (above) | Never reintroduced. `now()`/`sum_()` cover real needs; `server_default` accepts a plain string; a partial-index predicate accepts a plain string |
| `JSONB` | Not re-exported | `JSON` (Postgres `json`) by default; JSONB only where a query needs containment/GIN, declared in the migration (§ point 4) |
| `ARRAY` | Not re-exported | Child tables (`app.contact_tags`) — indexable and queryable anyway |
| `Enum` | Not re-exported | `String` + `CheckConstraint` for closed, safety-critical sets; plain `String` for extensible sets |
| `Time`, `Date` | Not re-exported | Working hours as minutes-from-midnight `Integer` (0–1440) — better for availability arithmetic and free of time-type/DST edge cases |
| `Float` | Not re-exported | `Numeric` (also the right choice for money and for confidence scores) |
| `BigInteger` | Not re-exported | `Integer` suffices for every planned column; where a BIGINT is genuinely needed, the migration declares it (§ point 4) |
| `LargeBinary` | Not re-exported | Not needed — no binary blob is stored in the database by design (recordings live in object storage, §16.1) |
| `UUID` type object | Not needed | `Mapped[uuid.UUID]` + `mapped_column(...)` maps to native `uuid` without any type import; the reference consumer does exactly this |
| Any async API | **Does not exist** — no `create_async_engine`, no `AsyncSession`, no `asyncpg` anywhere in `core`/`infra`/`api` | Phase 0 §4.2 / G-1: all DB work off the event loop via `asyncio.to_thread` |

Tenant/RLS integration point, verified:
`tenant_rls_statements(table, *, schema=None, tenant_column="tenant_id")`
returns `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, and a policy
`USING (tenant_column = NULLIF(current_setting('app.tenant_id', true), '')::uuid)`.
`tenant_session_scope(tenant_id)` sets `app.tenant_id` with a bound parameter
for the transaction. `FORCE` means even the table owner is subject to the
policy.

## Decision

1. **Two layers, one line between them.**
   - **Application layer** (models, services, tools, runtime): imports
     persistence primitives **only** through the product's own `voiceagent.db`
     module, which re-exports `infra.db`. It never imports `sqlalchemy`,
     `psycopg`, or any `sqlalchemy.dialects.*` name.
   - **Migration layer** (`voiceagent/migrations/versions/*.py`): imports
     `sqlalchemy as sa` and uses any PostgreSQL type or raw DDL it needs. This
     is the platform's own sanctioned pattern in `examples/reference-consumer/`.
     It is safe because a migration emits DDL under its own transaction; it
     never runs inside a tenant-scoped session and so cannot bypass a tenant
     policy.
2. **`func` and `text` are never reintroduced into the application layer**, by
   any route — not re-exported, not imported, not wrapped, not reached through
   `sqlalchemy.sql`. This is the single non-negotiable rule of this ADR.
3. **Product models are declared on `infra.db.Base`**, matching the reference
   consumer. The product does not define its own `DeclarativeBase` (which would
   require a direct SQLAlchemy import for no benefit).
4. **The migration is the source of truth for a column's physical type.** Where
   the needed PostgreSQL type is not exported by `infra.db`, the migration
   declares it and the model maps it with a compatible exported type (e.g. a
   `BIGINT` column mapped as `Mapped[int]`). The ORM type only needs to be
   compatible for bind/result handling — DDL is never generated from the models.
5. **`Base.metadata` is shared with SaaS-OS's own tables**, because core modules
   declare on the same base. Therefore: **Alembic autogenerate is disabled**
   (`target_metadata = None`, exactly as the reference consumer's `env.py` does)
   and `create_all()` is never called anywhere, in any environment, including
   tests. Migrations are hand-written. If autogenerate is ever wanted, it
   requires `include_schemas` plus an `include_object` filter restricted to
   schema `app`, introduced by its own ADR.
6. **Every product table is created by a migration that**: creates it in schema
   `app`; includes `tenant_id uuid NOT NULL REFERENCES core.tenants(id)`;
   applies `tenant_rls_statements(table, schema="app")`; indexes `tenant_id`;
   and issues the `GRANT`/`ALTER DEFAULT PRIVILEGES` statements for the
   application role — grants are the product's responsibility, not the
   platform's, as the reference consumer's migration demonstrates.
7. **The compatibility layer is one module, not a framework.**
   `voiceagent/db/__init__.py` contains: re-exports of the `infra.db` names the
   product uses; a small `tenant_table_args(...)` helper for the repeated
   `schema="app"` / index boilerplate; and nothing else. No session wrapper, no
   repository base class, no query builder, no second ORM. Its purpose is a
   single seam so that an upstream surface change on a future re-pin is a
   one-file fix rather than a repository-wide one.
8. **Self-imposed, mechanically enforced.** The product's own import-linter
   contracts forbid `sqlalchemy`/`psycopg` from every product module except
   `voiceagent.migrations`, and forbid `infra.db` imports outside
   `voiceagent.db`. CI fails on violation. A test asserts that neither `text`
   nor `func` is reachable from `voiceagent.db`.
9. **No upstream change is requested.** The gap is fully coverable, and asking
   SaaS-OS to widen a surface that was narrowed for documented security reasons
   would be the wrong request.

## Alternatives rejected

- **Import SQLAlchemy and `sqlalchemy.dialects.postgresql` directly in product
  models** — rejected. It would restore the `text`/`func` RLS-bypass capability
  class inside the product, in a codebase whose most sensitive data is call
  recordings and transcripts, in exchange for convenience that points 1 and 4
  already provide.
- **Ask SaaS-OS to export `JSONB`/`ARRAY`/`Enum`/`Time`** — rejected: modifying
  the dependency is forbidden (ADR-0001), and each missing type has an answer
  that is as good or better.
- **A product-owned ORM abstraction layer (repositories, unit-of-work, a typed
  query facade)** — rejected: a second framework to maintain, more surface than
  the problem, and it would obscure the `tenant_session_scope` boundary that is
  the isolation guarantee.
- **A separate product `DeclarativeBase`** — rejected: requires a direct
  SQLAlchemy import, diverges from the sanctioned fixture, and the only problem
  it would solve (shared metadata) is already solved by disabling autogenerate.

## What would be difficult to change later

Point 2. A single application-layer call site holding `text` or `func` makes
every subsequent author reasonably believe it is permitted, and the RLS-bypass
capability cannot be removed again by review — only by an audit of every query
in the codebase.

## Open verification tasks (not blockers)

- **JSONB round-trip**: if a JSONB column is ever introduced under point 4, an
  integration test must confirm that mapping it with `infra.db.JSON` round-trips
  correctly under psycopg3 (which may already deserialize `jsonb`), before that
  column ships. The default (`JSON`/`json`) avoids the question entirely.
- **Integer width**: confirm during P1.3 that no planned column exceeds 32-bit
  range; otherwise apply point 4.

## Related

SaaS-OS `infra/db/orm.py` (the security rationale), `infra/db/rls.py`,
`examples/reference-consumer/` (models vs migration split), SaaS-OS ADR-0002
(tenant isolation), ADR-0016 (independent migration histories);
`docs/PHASE-0-ARCHITECTURE.md` §2.4 G-1/G-4, §19 OD-4, §22.
