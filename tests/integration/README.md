# Integration tests

Everything under `tests/` outside this directory is hermetic: no PostgreSQL,
no Redis, no network. The tests here are the deliberate, documented
exception -- they need a real PostgreSQL instance with both SaaS-OS's own
migrations and this product's migrations applied, because the properties they
verify (Row-Level Security actually rejecting a cross-tenant read, a
composite foreign key actually rejecting a cross-tenant reference, the
`agent_versions_immutable` trigger actually rejecting a mutation) cannot be
observed any other way -- SQLite has no Row-Level Security, and mocking the
database would only prove the mock agrees with itself.

This mirrors the pinned SaaS-OS's own convention exactly
(`tests/infra/test_db_integration.py`, `pytest -m integration`, excluded from
the default run by `addopts` in `pyproject.toml`). No new testing convention
was invented for this product.

## Why no permanent local/CI harness exists yet

Phase 2.1's brief (§25) was explicit: if a real PostgreSQL instance is needed
and no harness exists, document that rather than silently introducing Docker
or a new infrastructure dependency into the repository's ordinary developer
workflow. Accordingly:

* **No `docker-compose.yml`** was added to this repository.
* **The CI `migrations-integration` job** (`.github/workflows/ci.yml`) *does*
  use a disposable `postgres:16-alpine` service container -- but that is not
  a new convention either: it is the exact pattern the pinned SaaS-OS's own
  `.github/workflows/ci.yml` `migrations` job already uses (an ephemeral,
  health-checked, torn-down-with-the-runner container, never a developer's
  own long-lived database).
* **This phase's own verification** (recorded in
  `docs/PHASE-2.1-STATUS.md`) was performed against a throwaway,
  manually-started `postgres:16-alpine` container, torn down immediately
  after -- not a database this repository now depends on existing.

## Running these tests locally

```bash
docker run -d --rm --name voiceagent-test-pg \
    -e POSTGRES_USER=saas_os -e POSTGRES_PASSWORD=devpassword \
    -e POSTGRES_DB=voiceagent -p 15432:5432 postgres:16-alpine

docker exec voiceagent-test-pg psql -U saas_os -d voiceagent -c "
    CREATE ROLE saas_os_app LOGIN PASSWORD 'devpassword'
        NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
    GRANT CONNECT ON DATABASE voiceagent TO saas_os_app;
"

export DATABASE_URL="postgresql+psycopg://saas_os_app:devpassword@127.0.0.1:15432/voiceagent"
export MIGRATIONS_DATABASE_URL="postgresql+psycopg://saas_os:devpassword@127.0.0.1:15432/voiceagent"
export REDIS_URL="redis://127.0.0.1:1/0"   # not actually connected to by these tests
export APP_DB_USER=saas_os_app
export ENVIRONMENT=test

saas-os-migrate upgrade   # the platform's own history, first (ADR-0016)
alembic upgrade head      # this product's history, second

pytest -m integration
```

**Connecting as the restricted `saas_os_app` role, not the schema-owning
role, is not optional** -- a superuser or `BYPASSRLS` role bypasses Row-Level
Security entirely regardless of `FORCE ROW LEVEL SECURITY`, which would make
every RLS assertion in this suite pass for the wrong reason.
