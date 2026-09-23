# Phase 2.17: Production Deployment & Release Validation

Checkpoint: Phase 2.16 (`c5e955e security: harden production readiness`) is
the branch's tip commit at the start of this phase. SaaS-OS remains pinned
and unmodified. No commit exists yet for this phase's own work; this
document is written before any staging or committing, per this phase's own
instruction.

This is not a feature-development phase. It closes concrete gaps between the
repository and a deployable production release: production configuration,
migration/release validation, startup/shutdown, runtime/worker release
behavior, frontend production build, health/readiness, deployment artifacts,
dependency/runtime compatibility, a narrow security re-validation, and
operational documentation. It does not modify `docs/PHASE-0-ARCHITECTURE.md`
or `docs/ADR/0010-one-frontend-multiple-user-contexts.md`, SaaS-OS, its pin,
or `pyproject.toml`.

## 1. Baseline

- HEAD at phase start: `c5e955e security: harden production readiness`.
- Phase 2.16 commit contents confirmed via `git show --stat c5e955e`:
  matches the file list recorded in `docs/PHASE-2.16-SECURITY-READINESS.md`
  (frontend session/tenant-context fix, CORS method fix, security headers,
  `0011_call_sessions_tenant_status_index` migration, FreeSWITCH ESL
  injection tests, heartbeat tests, tool-handler tests).
- Pre-existing working-tree state at phase start (confirmed via
  `git status --porcelain` before any Phase 2.17 change): exactly the two
  protected files --
  `M docs/PHASE-0-ARCHITECTURE.md` and
  `?? docs/ADR/0010-one-frontend-multiple-user-contexts.md`. Both are
  untouched by this phase; their working-tree content is exactly as it was
  at phase start.
- No clean/reset/restore/stash/amend/rebase/squash/history-rewrite was
  performed at any point in this phase.

## 2. Findings

### Finding 1 -- Migration `0011` revision id exceeds `alembic_version.version_num`'s column width (Critical, fixed)

- **Component**: `migrations/versions/0011_call_sessions_tenant_status_index.py`.
- **Issue**: The migration's `revision` identifier,
  `"0011_call_sessions_tenant_status_index"`, is 38 characters. Alembic's
  (and this pinned SaaS-OS's) `alembic_version` table defines
  `version_num` as `VARCHAR(32)`. Every other migration in this repository's
  history has a revision id of 29 characters or fewer.
- **Exploitability/reachability**: Not an attacker-facing issue -- an
  operational release defect. Reached by the ordinary, required release
  step `alembic upgrade head` against a real PostgreSQL database.
  `tests/test_migrations.py`'s existing hermetic suite only renders this
  migration's SQL offline (`alembic upgrade head --sql`), which never
  executes the `UPDATE alembic_version SET version_num=...` statement
  Alembic issues after a real upgrade -- so the offline suite could not
  have caught this, and had not.
- **Impact**: `alembic upgrade head` against a real database fails with
  `psycopg.errors.StringDataRightTruncation: value too long for type
  character varying(32)` on that `UPDATE`. The migration cannot be applied
  to any real deployment; the index this migration exists to add is never
  created, and the release step that runs it fails outright.
- **Evidence**: Reproduced against the local `voiceagent-test-pg` container
  (`postgres:16-alpine`) with the exact command
  `tests/integration/README.md` documents:
  ```
  sqlalchemy.exc.DataError: (psycopg.errors.StringDataRightTruncation) value too long for type character varying(32)
  [SQL: UPDATE alembic_version SET version_num='0011_call_sessions_tenant_status_index' WHERE alembic_version.version_num = '0010_call_ai_analyses']
  ```
  Confirmed via `psql` that the failed transaction rolled back cleanly: no
  index was created, `alembic_version` remained at `0010_call_ai_analyses`,
  no partial state.
- **Remediation**: Shortened the revision id to `"0011_call_sessions_index"`
  (25 characters). No DDL, index name, or column changed -- purely an
  Alembic metadata rename. Re-verified against the same real database: a
  full `alembic upgrade head` → `alembic downgrade -1` → `alembic upgrade
  head` cycle succeeds; `\d app.call_sessions` shows
  `"ix_call_sessions_tenant_status" btree (tenant_id, status)` present after
  upgrade and absent after downgrade. A regression test
  (`tests/test_migrations.py::test_every_revision_id_fits_in_the_alembic_version_column`)
  now asserts every migration's revision id is ≤32 characters, using
  Alembic's own `ScriptDirectory.walk_revisions()` -- it fails against the
  pre-fix revision id and passes against the fix.

### Finding 2 -- `tests/test_migrations.py` contains a detect-secrets false positive (Informational, not fixed)

- **Component**: `tests/test_migrations.py:25`, `_MIGRATION_ENVIRONMENT`'s
  `MIGRATIONS_DATABASE_URL` value
  (`postgresql+psycopg://owner:unused@127.0.0.1:1/unused`).
- **Issue**: `detect-secrets-hook --baseline .secrets.baseline` flags this
  line as a "Basic Auth Credentials" possible secret. It is a deliberately
  fake, non-routable (`127.0.0.1:1`) fixture value naming itself `unused`,
  used only to make Alembic render SQL offline in a hermetic test -- not a
  real credential, and pre-existing (confirmed via `git diff` that this
  phase's only change to this file is a new test function appended after
  line 53; this line predates Phase 2.17 entirely). It is not present in
  `.secrets.baseline`, so running the hook against this file in isolation
  reports it every time.
- **Exploitability/reachability**: None -- no real secret is present.
- **Impact**: A future `detect-secrets-hook` run scoped to this file (e.g.
  a pre-commit hook touching it) will report a false positive until
  suppressed.
- **Remediation**: Not changed this phase -- it is pre-existing content
  unrelated to production-deployment gaps, and touching it would be
  unrelated cleanup outside this phase's change discipline. Left as a
  documented, known false positive; an operator adding a `pragma:
  allowlist secret` comment or a baseline entry in a future, purpose-built
  change would resolve it.

### Finding 3 -- README.md's status header is stale (Informational, not fixed)

- **Component**: `README.md` ("Status: Phase 1 — foundation, IN
  PROGRESS", "There is no domain yet, by design").
- **Issue**: The repository is at Phase 2.17; the domain (calls, agents,
  contacts, workflows, runtime, telephony) has existed since Phase 2. The
  header was never updated as phases progressed.
- **Impact**: Cosmetic/documentation accuracy only -- every command in
  the README (`uvicorn voiceagent.api.asgi:app`, `alembic upgrade head`,
  `scripts/check-*.sh`) was independently verified working this phase (see
  §4, §5). A reader relying on the status header for the product's actual
  maturity would be misled; a reader following the commands would not be.
- **Remediation**: Not changed this phase -- rewriting the status section
  is a broader documentation change than this phase's brief scopes
  ("narrowly scoped... deployment/release fixes", not general doc
  maintenance), and is unrelated to any of the deployment gaps this phase
  targets. Flagged for a future, purpose-built documentation pass.

No other findings. The remaining workstreams (production configuration,
startup/shutdown, runtime/worker release behavior, health/readiness,
deployment artifacts, dependency/runtime compatibility, security
re-validation) were re-validated against the real local infrastructure and
against Phase 2.16's own properties, with no further defect found -- see §4
and §6.

## 3. Changes Made

| Path | Reason | Change |
|---|---|---|
| `migrations/versions/0011_call_sessions_tenant_status_index.py` | Finding 1 (Critical): `revision` id exceeded `alembic_version.version_num VARCHAR(32)`, breaking `alembic upgrade head` against a real database | Shortened `revision` from `"0011_call_sessions_tenant_status_index"` to `"0011_call_sessions_index"`; added a docstring paragraph recording the finding and fix. No DDL, index name, or filename changed. |
| `tests/test_migrations.py` | Regression coverage for Finding 1, per this phase's brief ("fix only that defect and add a focused regression test") | Added `test_every_revision_id_fits_in_the_alembic_version_column`, which walks every migration via Alembic's `ScriptDirectory` and asserts each `revision` id is ≤32 characters. |
| `docs/PHASE-2.17-RELEASE-VALIDATION.md` (this file) | Workstream J: production/release documentation for this phase's findings | New file. |

No other file was modified, staged, or created by this phase. Nothing was
renamed, moved, or deleted.

## 4. Deployment Validation

| Check | Status | Detail |
|---|---|---|
| API startup (`build_app()` / `voiceagent/api/asgi.py`) | PASS | `app = build_app()` at module scope, confirmed no I/O at import (no DB/Redis/secret/provider access) by reading `voiceagent/api/app.py` in full; construction invariants asserted by `tests/api/test_app.py` (in the 827-test hermetic run, §5). |
| DB migration validation | PASS WITH LIMITATION | Verified against real PostgreSQL (`voiceagent-test-pg`, local Docker): `saas-os-migrate upgrade` then `alembic upgrade head` succeed end-to-end; full upgrade→downgrade→re-upgrade cycle for `0011` verified clean (Finding 1). `alembic heads` confirms exactly one head. Limitation: only this local, ephemeral database was exercised, not a production-scale dataset. |
| Redis | PASS WITH LIMITATION | `voiceagent-test-redis` (local Docker) reachable and used by the integration suite (`tests/integration/test_redis_heartbeat_integration.py`, 3 tests, real Redis, all passing). Limitation: no production-scale Redis (clustering, persistence/eviction policy) was exercised -- out of scope for local validation. |
| Runtime (`CallRuntime`) | PASS WITH LIMITATION | Ownership/heartbeat/reconciliation/stuck-call logic re-confirmed advisory-only and non-authoritative by reading `voiceagent/runtime/heartbeat.py`, `reconciliation.py`, `stuck_calls.py` (unmodified this phase; module docstrings still state PostgreSQL is authoritative and no takeover is implemented). No runtime process entrypoint exists to start `CallRuntime` as a standalone process -- a pre-existing, explicitly documented Phase 2.14 architectural deferral (`docs/PHASE-2.14-STATUS.md` §12, §23), not a Phase 2.17 defect; see §6. |
| Workers (`FollowUpWorker`, `CallAiAnalysisWorker`) | PASS WITH LIMITATION | Both implement idempotent `start()` / cancel-and-await `shutdown()` (confirmed unmodified in `voiceagent/followups/worker.py`, `voiceagent/call_intelligence/worker.py`). Same missing-process-entrypoint limitation as the runtime, above. |
| Frontend production build | PASS | `npm run build` (`tsc --noEmit && vite build`) succeeds: 109 modules, `dist/index.html` + one JS/CSS asset each, no source maps emitted (default Vite behavior, confirmed no `sourcemap` setting in `vite.config.ts`), asset paths absolute (`/assets/...`), correct for a root-served SPA. |
| Health/readiness (`/healthz`, `/readyz`) | PASS | Re-confirmed via `tests/api/test_app.py`: `/healthz` always 200; `/readyz` reports dependency state without ever 500-ing or leaking connection detail, exercised in the 827-test hermetic run. No code change needed -- behavior already correct as of Phase 2.14/2.16. |
| Shutdown | PASS | Worker `shutdown()` is cancel-and-await, idempotent (unmodified, re-read this phase); ASGI lifespan/shutdown is owned by SaaS-OS's `build_platform_app()` (off-limits, already verified in prior phases). |
| Configuration validation | PASS | `Settings.__post_init__()` fails closed in production on `DEBUG=true` and on non-`https://` or wildcard CORS origins (re-read `voiceagent/config/settings.py` in full, unmodified). `.env.example` contains only placeholders/`CHANGE_ME` values, no real secret. |

## 5. Test Results

- **Backend hermetic suite**: `pytest -m "not integration" -q` (the
  project's default `addopts`) -- 827 tests collected
  (`pytest --collect-only -q`, summed per-file counts), full run completed
  with no failure/error markers, reached `[100%]`.
- **Backend integration suite**: `pytest -m integration -q`, run against
  real local PostgreSQL + Redis (`voiceagent-test-pg` on `127.0.0.1:15432`,
  `voiceagent-test-redis` on `127.0.0.1:16379`), connected as the
  restricted `saas_os_app` role (not a superuser/`BYPASSRLS` role) per
  `tests/integration/README.md` -- 241 tests collected, exit code 0, no
  failure/error markers.
  - Not run: FreeSWITCH-, real-OIDC-provider-, and real-AI-provider-credential-dependent
    paths -- no such infrastructure is available in this sandbox. These are
    genuinely absent dependencies (category: **unable to execute --
    infrastructure absent**), not weakened, bypassed, or faked. No test was
    classified as passed without actually running.
- **New regression test**: `tests/test_migrations.py::test_every_revision_id_fits_in_the_alembic_version_column`
  -- passes against the fix, and was confirmed to fail against the
  pre-fix revision id before the fix was applied.
- **Frontend tests**: `npm run test -- --run` (Vitest) -- 6 test files, 51
  tests, all passed.
- **Frontend typecheck**: `npm run typecheck` (`tsc --noEmit`) -- 0 errors.
- **Frontend build**: `npm run build` -- succeeds (see §4).
- **Frontend audit**: `npm audit` -- 0 vulnerabilities.
- **Ruff**: `ruff check .` -- all checks passed.
- **Ruff format**: `ruff format --check .` -- 312 files already formatted;
  1 pre-existing, unrelated formatting nit in a `docs/PHASE-2.10-STATUS.md`
  code block (a comment-spacing difference inside a fenced example, not
  executable code, not touched by any phase's actual source) -- not part of
  this phase's diff, not fixed (unrelated cleanup).
- **Pyright**: `pyright` -- 0 errors, 0 warnings, 0 informations.
- **import-linter**: `lint-imports` -- analyzed 193 files, 921
  dependencies; 8/8 architecture contracts kept, 0 broken.
- **detect-secrets**: `detect-secrets-hook --baseline .secrets.baseline`
  run against every file this phase touched. The migration file: clean. The
  test file: reports the pre-existing false positive documented as Finding
  2, unrelated to this phase's own addition. No real secret found anywhere
  in this phase's changes.
- **pip-audit**: `python -m pip_audit` (installed ephemerally into the
  venv, not added to `pyproject.toml`) -- no known vulnerabilities in any
  audited dependency; `saas-os` and `voiceagent` themselves are correctly
  skipped (not on PyPI).

## 6. Production Readiness Gaps

**Actual defects fixed this phase**:
- Migration `0011`'s revision id exceeding Alembic's version-column width
  (Finding 1) -- this would have broken every production deployment's
  migration step outright.

**Infrastructure-dependent validation gaps** (external to this repository,
cannot be closed from inside it):
- FreeSWITCH: no instance available in this sandbox. Telephony-provider
  contract tests (`tests/telephony/freeswitch/`) run hermetically against
  fakes; nothing exercises a real ESL/media connection. This was true
  before this phase and remains true after it.
- Real OIDC provider: SaaS-OS's `api.auth` flow is exercised against its
  own hermetic test doubles; no real identity provider was available to
  validate against in this sandbox.
- Real AI provider credentials: every AI-provider-facing code path in this
  product runs against `"fake"` providers by design (`AiProviderSettings`,
  `CallIntelligenceSettings` default to `"fake"`); no real STT/LLM/TTS or
  post-call-analysis vendor credential was available or used.
- Production-scale PostgreSQL/Redis: only a small local ephemeral instance
  of each was exercised (§4); no load, replication, or failover scenario
  was tested.

**Intentionally deferred work** (documented architectural decisions from
earlier phases, re-confirmed still accurate, not re-opened this phase):
- No standalone process entrypoint exists for `CallRuntime`,
  `FollowUpWorker`, or `CallAiAnalysisWorker` -- `scripts/` holds only
  `bootstrap_rbac.py`; there is no `Dockerfile` or `docker-compose.yml` in
  the repository. This is not an oversight: `docs/PHASE-2.14-STATUS.md` §12
  and §23 document this explicitly (no `EslConnection`/`MediaSocket` real
  implementation exists yet to run against, and tenant enumeration for a
  background worker process is an open architectural question SaaS-OS does
  not yet answer). Inventing an entrypoint script or a deployment
  platform this phase would be exactly the kind of "new deployment
  platform merely because it is convenient" and "feature development" this
  phase's own brief prohibits. Left as-is.
- `docs/PHASE-2.1-STATUS.md`'s own decision against a repository
  `docker-compose.yml` (`tests/integration/README.md` explains why) is
  unchanged.

**External infrastructure responsibilities** (belong to the deploying
operator, not this repository):
- TLS termination, process supervision/restart policy, and secret
  injection into the environment (`infra.secrets`) are all deployment-time
  concerns this repository deliberately does not own.
- Running `saas-os-migrate upgrade` then `alembic upgrade head`, in that
  order, before starting the API process, on every deployment.
- Provisioning the two distinct PostgreSQL roles (`APP_DB_USER`, a
  non-superuser/non-`BYPASSRLS` role for the application; a separate
  schema-owning role for `MIGRATIONS_DATABASE_URL`) -- the platform's own
  startup guard refuses to serve traffic under an unsafe role.

No overall readiness score or percentage is given, per this phase's own
instruction.

## 7. Git Safety

- No commit was made.
- No push was made.
- Nothing was staged (`git add` was never run).
- `docs/PHASE-0-ARCHITECTURE.md` and
  `docs/ADR/0010-one-frontend-multiple-user-contexts.md` were not read-modified;
  their working-tree content is exactly as it was at phase start.
- SaaS-OS and its pinned commit SHA (`ff550010e5eafecace7311038aadc99fcecfbe3d`)
  were not touched.
- `pyproject.toml` was not modified -- no change to it was found necessary.
- No file outside the three listed in §3 was changed. `frontend/dist/` was
  produced by `npm run build` as a normal, gitignored build artifact (`git
  status --porcelain frontend/` reports nothing); it is not a working-tree
  change and was left in place (not a Phase-2.17-created artifact requiring
  cleanup -- it is the expected, gitignored output of a command this
  phase's own brief asked to be run).
- No `--reset`, `--restore`, `--clean`, `--stash`, `--amend`, `--rebase`,
  or history-rewriting command was run at any point.

## 8. Recommendation

**READY FOR CHECKPOINT**

If checkpointed, the exact files to include:

```
migrations/versions/0011_call_sessions_tenant_status_index.py
tests/test_migrations.py
docs/PHASE-2.17-RELEASE-VALIDATION.md
```

(`docs/PHASE-0-ARCHITECTURE.md` and `docs/ADR/0010-one-frontend-multiple-user-contexts.md`
remain pre-existing, out-of-scope working-tree state -- not part of this
phase's checkpoint, and not staged by this phase.)
