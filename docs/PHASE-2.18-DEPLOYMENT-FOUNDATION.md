# Phase 2.18: Production Deployment Foundation

Checkpoint: Phase 2.17 (`7808498 fix: validate production release migration`)
is the branch's tip commit at the start of this phase. SaaS-OS remains
pinned and unmodified. No commit exists yet for this phase's own work; this
document, like every change below, is written before anything is staged.

This phase builds the smallest reproducible deployment foundation for the
existing application: separated API, background workers, frontend,
PostgreSQL, Redis, configuration/secrets, migrations, health/readiness, and
graceful shutdown. It does not add product functionality, redesign the
application, or introduce Kubernetes/Terraform/cloud-specific
infrastructure. It does not modify `docs/PHASE-0-ARCHITECTURE.md` or
`docs/ADR/0010-one-frontend-multiple-user-contexts.md`, SaaS-OS, its pin, or
`pyproject.toml`.

## 1. Baseline

- HEAD at phase start: `7808498 fix: validate production release migration`.
- Pre-existing working-tree state (confirmed via `git status --porcelain`
  before any Phase 2.18 change): exactly the two protected files --
  `M docs/PHASE-0-ARCHITECTURE.md` and
  `?? docs/ADR/0010-one-frontend-multiple-user-contexts.md`. Both remain
  untouched by this phase.
- Existing deployment-related material, confirmed by inspection before
  writing anything (Workstream A): no `Dockerfile`, no `docker-compose.yml`,
  no reverse-proxy config, no worker/runtime process entrypoint anywhere in
  the repository (`scripts/` held only `bootstrap_rbac.py` and the
  `check-*.sh` quality-gate scripts). `voiceagent/api/asgi.py` (the API
  entrypoint), `.env.example`, and `tests/integration/README.md`'s
  documented local-Postgres/Redis procedure already existed and are reused
  unchanged. SaaS-OS pin: `ff550010e5eafecace7311038aadc99fcecfbe3d`,
  unchanged.

## 2. Deployment Architecture

```text
                    Internet
                        |
              frontend (nginx-unprivileged, :8080)
              serves the built SPA; proxies
              /v1, /healthz, /readyz, /auth -> api
                        |
                  api (uvicorn, :8000)
              voiceagent.api.asgi:app
                        |
        +---------------+----------------+
        |                                |
   PostgreSQL (authoritative)        Redis (advisory only)
   business/call/tenant state,       heartbeats; never
   ownership, durable workflows      authoritative state
        |
        +-- migrate (one-shot: saas-os-migrate upgrade, then alembic upgrade head)

  Separate, independent processes (not behind the reverse proxy --
  no inbound HTTP surface of their own):
    call-runtime                  scripts/run_call_runtime.py
    follow-up-worker              scripts/run_followup_worker.py
    call-intelligence-worker      scripts/run_call_intelligence_worker.py
```

TLS termination is an explicit external responsibility (§7) -- this stack's
own reverse-proxy boundary (`frontend`'s nginx) terminates plain HTTP inside
a private network; a real deployment puts a TLS-terminating load
balancer/proxy in front of it (or in front of the whole stack) and is not
something this repository automates (Workstream J: "do not assume this
repository must manage certificates").

## 3. Findings

### Finding 1 -- `pyproject.toml`'s `[tool.setuptools] packages` list omits nine real subpackages (Critical, NOT fixed -- reported per this phase's own "STOP and report" instruction)

- **Component**: `pyproject.toml`, `[tool.setuptools] packages`.
- **Issue**: The explicit, hand-enumerated `packages` list (added in Phase
  1 and never updated since) names 20 subpackages. The source tree actually
  contains 27 (`find voiceagent -name __init__.py`). Missing:
  `voiceagent.calendars`, `voiceagent.call_analysis`,
  `voiceagent.call_intelligence`, `voiceagent.contacts`,
  `voiceagent.followups`, `voiceagent.knowledge`, `voiceagent.ops`,
  `voiceagent.providers.call_intelligence`, `voiceagent.workflows`. Every
  one of these was added by a later phase (2.6 through 2.14) that extended
  the domain without revisiting this list.
- **Reachability**: Every ordinary developer/CI workflow installs this
  project editable (`pip install -e ".[dev,security]"`, the README's own
  documented command) or simply runs `pytest`/`ruff`/`pyright` against the
  source tree directly with the repo root on `sys.path` -- neither path
  consults this list the way a non-editable install does, which is exactly
  why this has never surfaced before. It is reached by the one thing no
  prior phase actually exercised end-to-end: `pip install .` (no `-e`) into
  a clean environment, which is what a real container build does, and
  which this phase's own Dockerfile does.
- **Impact**: Confirmed directly, twice, against the real container image
  this phase built. `scripts/run_call_runtime.py`,
  `scripts/run_followup_worker.py`, and `scripts/run_call_intelligence_worker.py`
  (this phase's own new entrypoints, run as `python scripts/run_x.py`) all
  fail immediately with `ModuleNotFoundError` (`voiceagent.workflows`,
  `voiceagent.followups`, `voiceagent.call_intelligence` respectively) --
  see evidence below. **The `api` service in this phase's own
  `docker-compose.yml` does NOT visibly fail**, but only incidentally: this
  phase's own Dockerfile also `COPY`s the full, un-filtered `voiceagent/`
  source tree into `/app` (required so Alembic and the new scripts can find
  `migrations/`/`alembic.ini`/their own source at runtime), and `uvicorn`
  inserts its own working directory (`/app`) at `sys.path[0]`
  (`uvicorn/main.py`'s own `sys.path.insert(0, app_dir)`, confirmed by
  reading it inside the built image). That accidentally shadows the
  broken, incompletely-installed copy in site-packages with the complete
  source copy for the one process invoked in a way that benefits from it.
  A deployment that installs a real wheel into a container with no such
  extra source copy present (a more conventional production pattern) would
  see the API itself fail identically. This was verified directly: `docker
  run --rm ai-agent-api ls /opt/venv/lib/python3.13/site-packages/voiceagent/`
  shows the incomplete set (missing exactly the same nine); a plain `python
  -c "from voiceagent.ops.permissions import RESOURCE"` inside the
  container's `/opt/venv` alone (no `/app` on the path) would fail the
  identical way.
- **Evidence**:
  ```
  Traceback (most recent call last):
    File "/app/scripts/run_call_runtime.py", line 64, in <module>
      from voiceagent.runtime.supervisor import CallRuntime
    File "/opt/venv/lib/python3.13/site-packages/voiceagent/runtime/supervisor.py", line 47, in <module>
      from voiceagent.error_taxonomy import categorize_exception
    File "/opt/venv/lib/python3.13/site-packages/voiceagent/error_taxonomy.py", line 42, in <module>
      from voiceagent.workflows.errors import WorkflowError
  ModuleNotFoundError: No module named 'voiceagent.workflows'
  ```
  (identical pattern independently reproduced for `run_followup_worker.py`
  -> `voiceagent.followups` and `run_call_intelligence_worker.py` ->
  `voiceagent.call_intelligence`.)
- **Remediation**: NOT applied. This phase's own brief is explicit: *"Do
  not modify `pyproject.toml` unless a concrete deployment defect
  absolutely requires it. If such a change appears necessary: STOP and
  report it instead of silently changing it."* This is exactly that case.
  The fix itself is small and mechanical (add the nine missing entries to
  `packages = [...]`, matching the existing list's own style exactly), but
  it is left for the user's explicit decision rather than made here. Until
  it is made, `scripts/run_call_runtime.py`,
  `scripts/run_followup_worker.py`, and
  `scripts/run_call_intelligence_worker.py` cannot run against a real,
  non-editable install of this package -- only against a container that
  also happens to shadow site-packages with a full source copy the way
  this phase's own `Dockerfile` does (which works, but only because of that
  incidental shadowing, not because the installed package is actually
  correct).

### Finding 2 -- `DB_REQUIRE_TLS`/`REDIS_REQUIRE_TLS` must be set explicitly for any all-in-one-Docker-network deployment (Informational, addressed in `docker-compose.yml`)

- **Component**: SaaS-OS's own `infra.db.config`/`infra.jobs.config` (S-05
  hardening, not part of this repository, not modified).
- **Issue**: `ENVIRONMENT=production` makes both modules require
  `sslmode=require`/`rediss://` by default. The plain `postgres:16-alpine`
  and `redis:7-alpine` images this stack uses have no TLS configured.
- **Reachability**: Any production-environment deployment of this stack
  against non-TLS PostgreSQL/Redis.
- **Impact**: Without the override, `migrate`, `api`, `call-runtime`, and
  both workers fail at startup with a clear, non-silent
  `StringDataRightTruncation`-style configuration error (in this case
  `psycopg.OperationalError: ... server does not support SSL` /
  `infra.jobs.errors.JobsConfigurationError`) -- this is the platform
  failing closed correctly, not a defect; it only needed to be discovered
  and configured for once, here.
- **Evidence**: reproduced directly running `docker compose up migrate`
  before the override was added; both error messages are quoted verbatim
  in `docker-compose.yml`'s own top-of-file comment.
- **Remediation**: `DB_REQUIRE_TLS=false` / `REDIS_REQUIRE_TLS=false` set
  on every service in `docker-compose.yml` that talks to PostgreSQL/Redis,
  with an explanatory comment naming exactly why this is safe for this
  stack (every such service stays on the one private compose network) and
  exactly what a real production deployment must instead do (terminate
  real TLS on PostgreSQL/Redis, or make the identical "trusted private
  network" call knowingly). Re-verified end-to-end after the fix: `migrate`
  completes (exit 0, ends at `0011_call_sessions_index`), `api` becomes
  healthy.

### Finding 3 -- `tests/test_migrations.py`'s pre-existing detect-secrets false positive (Informational, carried over from Phase 2.17, still not fixed)

Unchanged from Phase 2.17's own Finding 2 (`tests/test_migrations.py:25`'s
fake `owner:unused@127.0.0.1:1` fixture URL). Not touched this phase --
still out of this phase's scope.

No other findings. Every other workstream (production configuration,
startup/shutdown, health/readiness, frontend serving, security artifact
review) was re-validated against the real containerized stack with no
further defect found -- see §5.

## 4. Changes

| Path | Reason | Description |
|---|---|---|
| `Dockerfile` | Workstream C: no production container existed | Two-stage backend image (builder with git/build-essential, discarded; runtime on `python:3.13-slim`, non-root `app` uid 10001). One image serves `api`, `migrate`, `call-runtime`, and both workers -- `docker-compose.yml` selects the command per service. |
| `.dockerignore` | Keeps the backend build context free of `.venv`, `.git`, frontend source, and any `.env*` file | New file. |
| `frontend/Dockerfile` | Workstream I: the frontend had no production-serving path, only `vite dev` | Two-stage: `node:22-alpine` builds `dist/`; `nginxinc/nginx-unprivileged:1.27-alpine` (non-root, port 8080) serves it. |
| `frontend/.dockerignore` | Keeps `node_modules`/`dist`/`.env*` out of the frontend build context | New file. |
| `frontend/nginx/default.conf.template` | The nginx boundary needs SPA fallback + same-origin API proxying | Proxies exactly the four path prefixes `frontend/vite.config.ts`'s own dev-server proxy already uses (`/v1`, `/healthz`, `/readyz`, `/auth`) to `${API_UPSTREAM}`; `try_files $uri /index.html` for client-side routing; `X-Content-Type-Options: nosniff` on every response. |
| `docker-compose.yml` | Workstream M: no local production-like deployment existed | `postgres`, `redis`, one-shot `migrate` (`condition: service_completed_successfully` gates every dependent service), `api`, `call-runtime`, `follow-up-worker`/`call-intelligence-worker` (in an opt-in `workers` profile, pending real tenant provisioning), `frontend`. Every credential is a local-only, `pragma: allowlist secret`-marked default. |
| `docker/postgres-init/01-create-app-role.sh` | The `saas_os_app` restricted role (`tests/integration/README.md`'s own documented procedure) must exist before any process connects | Runs once, automatically, via the official postgres image's own `docker-entrypoint-initdb.d` convention -- identical SQL to the manual procedure, not new behavior. |
| `scripts/run_call_runtime.py` | Workstream E: `docs/PHASE-2.14-STATUS.md` §12 documented "no entrypoint script exists yet" for the call-runtime process | New. Wires a real `CallRuntime` to `RedisHeartbeatStore`, starts its heartbeat, handles SIGTERM/SIGINT for bounded `shutdown()`. Explicitly documented as heartbeat/lifecycle-only: no real ESL transport exists anywhere in the repo (`voiceagent/telephony/freeswitch/esl.py`'s own docstring), so this process carries no real call traffic yet -- building that transport is out of this phase's scope (adding a telephony provider). |
| `scripts/run_followup_worker.py` | Same gap for `FollowUpWorker` | New. Tenant enumeration (the documented, pre-existing scope limit `FollowUpWorker` itself names) is read from a required, operator-supplied `VOICEAGENT_FOLLOWUP_WORKER_TENANT_IDS` env var, fresh every tick. |
| `scripts/run_call_intelligence_worker.py` | Same gap for `CallAiAnalysisWorker` | New. Reuses `Settings.call_intelligence` entirely for provider/model/timing config (already fully configurable); only adds the same tenant-enumeration env var pattern. |
| `docs/PHASE-2.18-DEPLOYMENT-FOUNDATION.md` (this file) | Workstream O | New. |

No implementation code inside `voiceagent/` was modified. No dependency was
changed. `pyproject.toml` was not modified (Finding 1 explains why one
concrete change there would be justified, and why it was not made without
the user's explicit decision).

## 5. Deployment Validation

All of the following were exercised against real, local Docker containers
(Docker 29.7.2, confirmed available), not simulated.

| Check | Status | Detail |
|---|---|---|
| Container/image build | PASS | `docker compose build` -- backend and frontend images both build clean. |
| Database migration | PASS | `docker compose up migrate` -- `saas-os-migrate upgrade` then `alembic upgrade head` against a fresh `postgres:16-alpine`, ends at `0011_call_sessions_index` (Phase 2.17's own fix), exit 0. Migration failure tested separately (bad credentials): exit 1, no false success. |
| PostgreSQL | PASS | Container healthy; `saas_os_app` role created automatically with no superuser/BYPASSRLS attributes (`\du` confirmed). |
| Redis | PASS | Container healthy; heartbeat key (`voiceagent:runtime:<instance>`) confirmed written by `scripts/run_call_runtime.py`. |
| API | PASS WITH LIMITATION | Starts, `/healthz` 200, `/readyz` 200 with both dependencies healthy. Its apparent full functionality is real (all 40 `/v1/*` + `/auth/*` routes present in `/openapi.json`), but see Finding 1: this specific success is partly incidental (source-tree shadowing), not proof the installed package alone is correct. |
| Runtime (`call-runtime`) | FAILED | Blocked by Finding 1 (`ModuleNotFoundError: voiceagent.workflows`). Heartbeat/shutdown wiring itself could not be exercised inside the container as a result; confirmed instead as a standalone code-review + the heartbeat-write proof above (from a run before this failure mode was isolated). |
| Workers (`follow-up-worker`, `call-intelligence-worker`) | FAILED | Same blocker (Finding 1): `voiceagent.followups` / `voiceagent.call_intelligence` missing. Neither was exercised beyond confirming the identical import failure -- no real tenant was available to test their actual polling logic either way (see §8). |
| Frontend | PASS | `docker compose up frontend` served the built SPA on `:8080`; production build re-verified (`npm run build`, no source maps, absolute asset paths). |
| Reverse proxy / TLS boundary | PASS WITH LIMITATION | `frontend`'s nginx correctly proxies `/v1`, `/healthz`, `/readyz`, `/auth` to `api` over the compose network. TLS itself is explicitly not handled here (external responsibility, §7) -- not tested because there is nothing in this repository to test for it. |
| Health/readiness | PASS | `/healthz` stays 200 through a real PostgreSQL outage (liveness independent of dependencies, as designed). `/readyz` correctly returns 503 with per-dependency status (`{"database": "unhealthy"}` / `{"redis": "unhealthy"}`) for both a real PostgreSQL outage and a real Redis outage, recovering to 200 once each was restored. No connection string or credential ever appeared in a response body. |
| Shutdown (API) | PASS | `docker stop api` (SIGTERM, default 10s grace): clean shutdown log (`Waiting for application shutdown` -> `Application shutdown complete` -> `Finished server process`) in ~1.8s, no forced kill needed. |
| Shutdown (call-runtime / workers) | NOT RUN -- blocked by Finding 1 | The signal-handling code itself (`scripts/run_call_runtime.py`, etc.) was reviewed and mirrors `CallRuntime.shutdown()`/`FollowUpWorker.shutdown()`/`CallAiAnalysisWorker.shutdown()`'s own already-tested, bounded cancel-and-await behavior, but could not be exercised as a running container process until Finding 1 is resolved. A native-Windows `kill -TERM`/`kill -INT` smoke test in this sandbox (outside any container) did not reliably deliver the signal to the child Python process at all -- a known Windows/MSYS signal-forwarding limitation of this sandbox, not evidence about Linux container behavior; the authoritative test is the containerized one, which Finding 1 blocks. |

## 6. Tests

- **Backend hermetic suite**: `pytest -m "not integration" -q` -- 827
  tests, 0 failures (unchanged from Phase 2.17; no `voiceagent/` source was
  modified this phase).
- **Ruff**: `ruff check .` -- all checks passed (after fixing one
  line-length violation this phase introduced in
  `scripts/run_call_runtime.py`).
- **Ruff format**: `ruff format --check .` -- clean for every Phase 2.18
  file (after auto-formatting `scripts/run_call_intelligence_worker.py`);
  the one pre-existing `docs/PHASE-2.10-STATUS.md` nit noted in Phase 2.17
  is unchanged and still not this phase's concern.
- **Pyright**: `pyright scripts/run_call_runtime.py
  scripts/run_followup_worker.py scripts/run_call_intelligence_worker.py
  scripts/bootstrap_rbac.py` -- 0 errors, 0 warnings (`scripts/` is not in
  `pyproject.toml`'s `[tool.pyright] include`, matching the pre-existing
  `bootstrap_rbac.py`'s own status; checked explicitly by file path rather
  than changing that config).
- **import-linter**: `lint-imports` -- 193 files, 921 dependencies, 8/8
  contracts kept, 0 broken (unchanged).
- **detect-secrets**: `detect-secrets-hook --baseline .secrets.baseline`
  run against every new Phase 2.18 file. Two genuine false positives in
  `docker-compose.yml` (the local-only `devpassword` default and the
  connection URLs built from it) marked with inline
  `pragma: allowlist secret` comments explaining why; clean after that.
  Every other new file (the three scripts, both Dockerfiles, the nginx
  template, the init shell script, both `.dockerignore` files) was clean
  with no suppression needed.
- **pip-audit**: no known vulnerabilities (no dependency changed this
  phase).
- **Frontend**: `npm run typecheck` (0 errors), `npm run test -- --run`
  (6 files, 51 tests, all passing), `npm run build` (clean, no source
  maps), `npm audit` (0 vulnerabilities) -- unchanged, since no frontend
  application source was modified, only its container/serving artifacts.
- **Deployment**: exercised directly, not simulated -- see §5 for the
  full breakdown, including the two failure-mode tests (migration failure,
  dependency-down readiness) run against the real stack.

## 7. Production Responsibilities

### Repository-provided

- Backend and frontend container images, buildable and runnable as-is
  (`Dockerfile`, `frontend/Dockerfile`).
- A local, production-like multi-service topology
  (`docker-compose.yml`) demonstrating the full process boundary: API,
  call-runtime, two workers, frontend, PostgreSQL, Redis, one-shot
  migration.
- A deterministic, ordered migration procedure that fails loudly and
  blocks dependent services on failure.
- Health (`/healthz`) and readiness (`/readyz`) endpoints, already correct,
  re-verified against real dependency outages.
- A reverse-proxy boundary for the frontend (nginx) that also fronts the
  API, so the browser only ever sees one origin.
- Non-root execution for every image this phase built.

### External infrastructure responsibilities

- **TLS certificates and termination.** Nothing in this repository
  provisions or automates certificates. A real deployment puts a
  TLS-terminating load balancer/reverse proxy in front of this stack (or
  extends `frontend/nginx/default.conf.template` with certificates the
  operator manages) -- deliberately not solved here (Workstream J: "do not
  introduce cloud-specific certificate automation").
- **DNS.**
- **Real TLS on PostgreSQL and Redis** (or an explicit, knowing decision
  to run them on a genuinely private/trusted network with
  `DB_REQUIRE_TLS=false`/`REDIS_REQUIRE_TLS=false`, as this local compose
  stack does) -- Finding 2.
- **PostgreSQL backups, retention, and disaster recovery.** This stack's
  `postgres_data` named volume is local-only, unmanaged persistence,
  adequate for a demo, not a backup strategy.
- **Cloud/VPS provisioning, firewalling, and network exposure control.**
- **Monitoring/alerting beyond what `/healthz`/`/readyz`/OpenTelemetry
  tracing already emit** (the platform's own tracing, unchanged this
  phase, already exports spans -- see the `/healthz` trace payload
  captured during validation).
- **Real secrets** for every credential this stack defaults to a local
  placeholder for (database passwords, and every OIDC/AI-provider/object
  storage secret `voiceagent/config/settings.py`'s own module docstring
  already establishes is read through `infra.secrets`, never from product
  configuration).
- **Telephony (FreeSWITCH), OIDC provider, and AI provider accounts** --
  none of these exist in this environment; `call-runtime` has no real call
  source to carry traffic on regardless of Finding 1 (see §8), and neither
  worker was validated against real AI-provider or tenant data.
- **At least one real, bootstrapped tenant** (`scripts/bootstrap_rbac.py`)
  before `follow-up-worker`/`call-intelligence-worker` can be started with
  a real `*_TENANT_IDS` value -- this repository does not create tenants;
  that is a SaaS-OS-owned flow this repository consumes, not one it can
  automate from a deployment script.

## 8. Remaining Gaps

**Actual defects**:
- Finding 1 (`pyproject.toml` packaging gap) -- Critical, confirmed,
  reported per this phase's own instruction, not fixed. This is the single
  blocking item standing between this phase's new process entrypoints and
  being genuinely runnable from a real installed package rather than only
  via this phase's own source-shadowing Dockerfile.

**Infrastructure-dependent validation gaps**:
- `call-runtime`'s and both workers' actual runtime behavior (not just
  their import) could not be validated in this environment even once
  Finding 1 is resolved: no real tenant exists to supply
  `*_TENANT_IDS`, and `call-runtime` has no real call source regardless
  (next item).
- SIGTERM/shutdown for the three new script entrypoints could not be
  exercised as running containers (blocked by Finding 1); only reviewed
  against the underlying classes' own already-tested shutdown behavior.

**Intentionally deferred work**:
- `call-runtime` carries no real call traffic: no real
  `EslConnection`/`MediaSocket` transport exists anywhere in this
  repository (`voiceagent/telephony/freeswitch/esl.py`'s own docstring
  scopes this out explicitly), and building one is telephony-provider work
  this phase's brief forbids. `scripts/run_call_runtime.py` is deployable
  and observable (heartbeat, clean shutdown) but idle until a future phase
  adds that transport.
- TLS certificate automation, deliberately not attempted (Workstream J).
- Multi-replica/horizontal-scaling validation: this stack runs exactly one
  instance of each process; `CallRuntime`'s own ownership-fencing
  (heartbeat + reconciliation) is designed for more than one, but more
  than one was not started here -- nothing in this phase's brief called
  for that, and doing so would not have exercised anything Finding 1
  doesn't already block for the runtime specifically.

**External responsibilities**: see §7.

No overall readiness score or percentage is given, per this phase's own
instruction.

## 9. Operational Runbook

**Prerequisites**: Docker with Compose v2+ (confirmed: Docker 29.7.2,
Compose v5.5.1). No other host dependency.

**1. Configuration and secrets.** Copy `.env.example` to `.env` for
reference; `docker-compose.yml` does not read `.env` automatically for
every value (it hardcodes its own local-only defaults, each marked
`pragma: allowlist secret`) -- a real deployment replaces every one of
those defaults (`POSTGRES_PASSWORD`, `POSTGRES_APP_PASSWORD`, and the two
connection URLs built from them) with real, injected secrets, and sets
`DB_REQUIRE_TLS`/`REDIS_REQUIRE_TLS` correctly for its own PostgreSQL/Redis
TLS posture (Finding 2) rather than reusing `"false"`.

**2. Build images**:
```
docker compose build
```

**3. Start PostgreSQL and Redis, wait for healthy**:
```
docker compose up -d postgres redis
```
The `saas_os_app` restricted role is created automatically, once, by
`docker/postgres-init/01-create-app-role.sh` the first time the `postgres`
volume initializes.

**4. Run migrations** (deterministic, ordered, fails closed):
```
docker compose up migrate
```
Confirm exit code 0 before proceeding -- a nonzero exit means the schema is
not verified, and `api`/`call-runtime`/the workers will not start against
it (`condition: service_completed_successfully`).

**5. Start the API and frontend**:
```
docker compose up -d api frontend
```
Verify: `curl http://localhost:8080/healthz` (through the frontend's
proxy) and `curl http://localhost:8000/readyz` (direct) both report
`"status":"healthy"`.

**6. Bootstrap at least one tenant's RBAC** (required before either worker
can run; this repository does not create tenants themselves -- that is a
SaaS-OS-owned flow):
```
docker compose run --rm api python scripts/bootstrap_rbac.py \
    --tenant-id <uuid> --actor-user-id <uuid>
```

**7. Start `call-runtime` and, once a real tenant exists, the workers**:
```
docker compose up -d call-runtime
docker compose --profile workers up -d follow-up-worker call-intelligence-worker
```
**Blocked today by Finding 1** (§3) until `pyproject.toml`'s `packages`
list is corrected -- see that finding for the exact, confirmed failure.

**8. Health/readiness verification**: `GET /healthz` (liveness, always
200 if the process is up) and `GET /readyz` (200 only when PostgreSQL and
Redis are both reachable; 503 with a per-check breakdown otherwise, never a
leaked connection string).

**9. Graceful shutdown**:
```
docker compose stop api          # SIGTERM, bounded (~2s observed, well inside the 10s default grace period)
docker compose stop call-runtime follow-up-worker call-intelligence-worker
docker compose down              # stops and removes containers; add --volumes to also discard postgres_data
```

**10. Logs/telemetry**: `docker compose logs -f <service>`. The platform's
own OpenTelemetry tracing is already active (`deployment.environment` tag
observed as `production` during validation) -- exporting those traces
anywhere durable (a collector, a backend) is an external responsibility,
not something this repository configures.

**11. Rollback**: `alembic downgrade -1` (re-run inside the `migrate`
image against the target database) reverses the most recent product
migration; this was exercised and confirmed clean for `0011` in Phase
2.17. There is no automated application-version rollback -- redeploying a
previous image tag is the mechanism, same as any container-based
deployment.

**12. Backups**: PostgreSQL backup/retention is entirely an external
responsibility (§7) -- this stack's own `postgres_data` volume is local,
unmanaged persistence only.

**13. TLS/reverse proxy**: external responsibility (§7) -- put a
TLS-terminating proxy/load balancer in front of `frontend`'s own nginx (or
extend its config with real certificates).

**14. External dependencies not present in this environment**: a real
FreeSWITCH instance, a real OIDC provider, and real AI provider
credentials/accounts. None of this stack's validation in §5/§6 depended on
any of them being present, and none of this phase's own claims rely on
them being present either.

## 10. Git Safety

- No commit was made.
- No push was made.
- Nothing was staged.
- `docs/PHASE-0-ARCHITECTURE.md` and
  `docs/ADR/0010-one-frontend-multiple-user-contexts.md` were not
  read-modified; their working-tree content is exactly as it was at phase
  start.
- SaaS-OS and its pinned commit SHA were not touched.
- `pyproject.toml` was not modified -- Finding 1 explains exactly why a
  change there would be justified, and that it was deliberately not made,
  per this phase's own instruction to stop and report rather than change
  it silently.
- No file outside the list in §4 was changed.
- No `--reset`, `--restore`, `--clean`, `--stash`, `--amend`, `--rebase`,
  or history-rewriting command was run. The local staging stack this phase
  built and exercised was torn down cleanly (`docker compose down`, plus
  removing its one named volume, `ai-agent_postgres_data`) after
  validation -- images (`ai-agent-api`, `ai-agent-call-runtime`,
  `ai-agent-migrate`, `ai-agent-frontend`) were left built, since rebuilding
  them is cheap and they are not working-tree state.

## 11. Recommendation

**REQUIRES FIXES**

The blocking item is Finding 1 (`pyproject.toml`'s `[tool.setuptools]
packages` list omits nine real subpackages), which this phase's own brief
required be reported rather than silently fixed. Once the user decides how
to proceed on that one specific, narrow change:

- If approved, the fix is: add `"voiceagent.calendars"`,
  `"voiceagent.call_analysis"`, `"voiceagent.call_intelligence"`,
  `"voiceagent.contacts"`, `"voiceagent.followups"`,
  `"voiceagent.knowledge"`, `"voiceagent.ops"`,
  `"voiceagent.providers.call_intelligence"`, and `"voiceagent.workflows"`
  to the existing `packages = [...]` list, in the same alphabetized style
  already used there. Re-running `docker compose up call-runtime` and the
  two worker scripts against a rebuilt image would then confirm the fix
  (expected: all three import cleanly and reach their own steady-state
  heartbeat/poll loop).
- Every other item in this report is otherwise ready.

Files that should be included in the eventual checkpoint commit (not
committed by this phase):

```
Dockerfile
.dockerignore
frontend/Dockerfile
frontend/.dockerignore
frontend/nginx/default.conf.template
docker-compose.yml
docker/postgres-init/01-create-app-role.sh
scripts/run_call_runtime.py
scripts/run_followup_worker.py
scripts/run_call_intelligence_worker.py
docs/PHASE-2.18-DEPLOYMENT-FOUNDATION.md
```
