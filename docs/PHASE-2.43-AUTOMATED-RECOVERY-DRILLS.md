# Phase 2.43 — Automated Disaster-Recovery Drills

## 1. What this phase does NOT change

* `scripts/db_backup.sh` and `scripts/db_restore.sh` (Phase 2.37) --
  unmodified. The drill invokes `db_restore.sh` as a subprocess exactly as
  any operator would.
* `scripts/offhost_backup.py` (Phase 2.42) -- unmodified. The drill reuses
  its manifest format and `MANIFEST_VERSION` constant; it never calls its
  transport, retention, or locking code.
* The database schema and Alembic migration history -- untouched.
* Row-Level Security and tenant isolation -- enforced, never bypassed or
  weakened; the drill's own validation step runs the real
  `tests/integration/test_domain_rls_integration.py` suite unmodified.
* No scheduler, recurring worker, or new infrastructure dependency was
  introduced (see §10, Non-goals).

## 2. What this phase adds

`scripts/db_recovery_drill.py`: a CI-invoked command that proves, on every
push/PR via `.github/workflows/ci.yml`'s new `recovery-drill` job, that a
real backup artifact actually restores into a working database --
Phase 2.37's own Limitations section called this gap out directly ("No
automated backup verification job"), and Phase 2.42's Limitations section
restated it ("Backup retention is not a substitute for restore drills").

## 3. The target safety boundary

**Hostname, database name, and connection-string shape do not prove a
PostgreSQL instance is disposable.** An operator's own laptop can be named
`localhost` too, and a manifest field can claim anything. This phase does
not pretend otherwise.

What actually makes the drill's target safe is architectural, not
something `db_recovery_drill.py` can observe from a connection string: the
`recovery-drill` CI job stands up its *own* two job-scoped, ephemeral
`postgres:16-alpine` service containers (`postgres-source`, `postgres-
target`) -- created fresh for that job, reachable by nothing else, torn
down with the runner. This is the identical pattern the pre-existing
`migrations-integration` job already uses, not a new convention.

`db_recovery_drill.py` itself never accepts an arbitrary externally-supplied
database URL and infers safety from it. It connects using only a
dedicated `DRILL_TARGET_*` configuration namespace (see §5), and before any
destructive action:

1. **Requires `DRILL_MODE=true`** as an explicit, unambiguous opt-in --
   missing or any other value is a configuration error, not a default.
2. **Refuses to restore into a target whose `app` schema already contains
   any object.** `SELECT count(*) FROM pg_class ... WHERE nspname = 'app'`
   must return `0`. There is no override flag, environment variable, or
   CLI argument that bypasses this -- the check function
   (`_assert_schema_empty`) takes exactly one argument, the count.
3. **Stamps a drill-owned marker** (`_drill.ownership`, a schema entirely
   separate from `app`) immediately after confirming emptiness, recording
   this run's own id as supplementary evidence in the report.

Neither (2) nor (3) is, by itself, proof the target is disposable
infrastructure -- that guarantee is the CI job's ephemeral container
lifecycle (item one, above). If this drill is ever run outside that CI
job (e.g. an operator against staging), the operator is the one assuming
responsibility for pointing `DRILL_TARGET_*` at a target whose entire
lifecycle is genuinely drill-scoped -- the missing prerequisite this
module cannot supply on its own is exactly that external guarantee.

## 4. Credential separation

`db_recovery_drill.py`'s `load_config()` never reads `DATABASE_URL`,
`MIGRATIONS_DATABASE_URL`, or `PGPASSWORD` for the restore target -- those
names are reserved for production/source use throughout this repository.
It additionally refuses to start (`ConfigError`) if a `DRILL_TARGET_*`
password equals the current `PGPASSWORD`, or if `DRILL_TARGET_HOST`/
`DRILL_TARGET_DB` matches `DATABASE_URL`'s or `MIGRATIONS_DATABASE_URL`'s
host/database -- a defense-in-depth check, not the primary safety
mechanism (§3), that only catches an operator pasting a production value
into the wrong variable.

Every subprocess the drill launches gets a freshly built, minimal
environment -- never `os.environ.copy()`:

* `db_restore.sh` receives only `PGPASSWORD=<DRILL_TARGET_OWNER_PASSWORD>`
  plus `PATH`/`HOME`/etc. -- never a production credential, even if one is
  sitting in the parent process's own environment.
* The tenant-isolation `pytest` subprocess receives `DATABASE_URL`/
  `MIGRATIONS_DATABASE_URL` built fresh from `DRILL_TARGET_*` -- it reuses
  the real `tests/integration/test_domain_rls_integration.py` suite, which
  itself reads those names, but the *values* it sees are the drill
  target's own, never read from the parent environment.

`tests/ops/test_recovery_drill.py` proves this by poisoning the parent
environment with fake production values and asserting neither subprocess
environment nor the structured report ever contains them.

## 5. Configuration

| Variable | Required | Meaning |
|---|---|---|
| `DRILL_MODE` | yes | must be exactly `true` |
| `DRILL_TARGET_HOST` / `_PORT` / `_DB` | yes | the dedicated drill target |
| `DRILL_TARGET_OWNER_USER` / `_OWNER_PASSWORD` | yes | schema-owning role, used by `db_restore.sh` |
| `DRILL_TARGET_APP_USER` / `_APP_PASSWORD` | yes | restricted role, used only to build the tenant-isolation subprocess's `DATABASE_URL` |
| `DRILL_RESTORE_TIMEOUT_SECONDS` | no (default `120`) | bounds `db_restore.sh` |
| `DRILL_VALIDATION_TIMEOUT_SECONDS` | no (default `300`) | bounds the RLS-suite subprocess |

CLI: `python scripts/db_recovery_drill.py drill --dump-path <path>
--output-dir <dir>` (`--meta-path`/`--manifest-path`/
`--manifest-checksum-path` default to the dump's own sidecar names, the
same convention `offhost_backup.py` establishes).

## 6. Artifact verification

Before any restore, `verify_artifact()`:

1. Confirms the dump file exists and is non-empty, and recomputes its
   sha256.
2. Confirms the dump's own `.meta.json` sidecar (`db_backup.sh`'s output)
   agrees on `sha256`/`size_bytes`.
3. Confirms the Phase 2.42 manifest's detached checksum sidecar
   (`.manifest.json.sha256`) matches the manifest file's own sha256.
4. Parses the manifest, requires `backup_format_version`, `backup_id`,
   `sha256`, `size_bytes`, `dump_filename`, `database`, and that
   `backup_format_version` equals `offhost_backup.MANIFEST_VERSION`
   (imported, never re-literalled).
5. Cross-checks `manifest["sha256"]`/`["size_bytes"]`/`["dump_filename"]`
   against the actual artifact.

Any mismatch anywhere in this chain -- a truncated dump, a corrupted
manifest, a manifest pointing at a different filename -- fails the drill
before `db_restore.sh` is ever invoked. The Phase 2.42 manifest format
carries no host/database/destination field at all; `verify_artifact()`
returns only an `ArtifactInfo` (filename, backup id, checksum, size) --
there is no code path through which a manifest could select or redirect
the restore target.

## 7. Validation performed after restore

1. **Migration revision**: the restored database's `alembic_version` row
   must equal this repository's own Alembic head (`ScriptDirectory.
   get_heads()`), computed from `alembic.ini`/`migrations/`, not asserted
   from the manifest's advisory `migration_revision` field.
2. **Relational/schema integrity**: every foreign key in the `app` schema
   must be `convalidated` (no `NOT VALID` constraint survived the
   restore); every base table in `app` must have Row-Level Security
   enabled and forced, except `app.inbound_call_routes` (the one
   deliberate Phase 2.22 exception), which must have neither.
3. **Tenant isolation and application-boundary validation**: the full,
   unmodified `tests/integration/test_domain_rls_integration.py` suite is
   run against the restored target via a subprocess `pytest -m
   integration`. This one suite is deliberately reused for both
   properties at once -- it exercises the real `voiceagent` service layer
   (`create_agent`, `tenant_session_scope`, etc.), not raw SQL, which is
   exactly Phase 2.37 §8/§9's own precedent for "proof recovery produced a
   database the application can actually use."

A failure at any step stops the drill immediately; every later step is
recorded `"skipped"` in the report, never silently omitted, and the drill
never falls back to retrying restoration or trying a second target.

## 8. Report format

`run_drill()` returns, and the CLI writes to `<output-dir>/recovery-drill-
<drill_id>.json`:

```json
{
  "drill_id": "...",
  "backup_id": "...",
  "artifact": {"dump_filename": "...", "sha256": "...", "size_bytes": 123},
  "started_at_utc": "...",
  "finished_at_utc": "...",
  "duration_seconds": 12.3,
  "target_isolation_evidence": {
    "host": "...", "port": "...", "database": "...",
    "ownership_marker_id": "..."
  },
  "steps": {
    "artifact_verification": {"status": "passed|failed|skipped", "detail": "..."},
    "target_safety_precondition": {...},
    "restore": {...},
    "migration_check": {...},
    "schema_integrity_check": {...},
    "tenant_isolation_check": {...}
  },
  "verdict": "PASS|FAIL",
  "failure": {"step": "...", "reason": "..."} | null
}
```

No password, token, connection string, or raw environment value is ever
written: `_scrub_secrets()` strips both `DRILL_TARGET_*` passwords from any
subprocess output or exception message before it reaches `steps`/
`failure`, and `target_isolation_evidence` carries only host/port/database
(not credentials). The verdict is computed before the report is written,
so a failed write (`OSError`) prints the already-determined verdict to
stderr/stdout and exits with the correct code -- a report-write failure
can never turn a FAIL into an apparent PASS.

## 9. Failure behavior

Every induced failure stops before the next destructive step and produces
`"verdict": "FAIL"`:

* Missing/truncated/corrupt artifact, invalid or tampered manifest --
  caught in `verify_artifact()`, before any database connection.
* Non-empty target schema -- caught in `_assert_schema_empty()`, before
  `db_restore.sh` is ever invoked (proved by
  `tests/ops/test_recovery_drill.py::test_run_drill_fails_closed_before_any_destructive_subprocess_call`,
  which asserts zero subprocess calls occurred).
* `db_restore.sh` non-zero exit, or a timeout on any subprocess step --
  `DrillError`, no retry, no fallback target.
* Migration mismatch, invalid constraint, inconsistent RLS flags, or the
  RLS suite itself failing -- each stops the pipeline at that step.
* Any other, unanticipated exception (a raw driver error, for instance) --
  `run_drill()`'s outer `except Exception` catches it too, scrubs it through
  the same `_scrub_secrets()` path, records it as a `"failed"` step (the
  step in flight when it was raised), and still writes a report -- an
  audit-found gap where only `DrillError` was originally caught, fixed and
  covered by
  `tests/ops/test_recovery_drill.py::test_run_drill_catches_unexpected_non_drill_error_and_still_fails_closed`.

## 10. Operational design / non-goals

CI-only, operator-triggered via the normal push/PR pipeline -- no
scheduler, cron, or recurring worker was introduced; the smallest design
that proves recovery works on every change. Explicitly out of scope for
this phase:

* Point-in-time recovery / WAL archiving.
* Any cloud-specific transport (the drill's backup step reuses Phase
  2.42's local "command" transport with
  `OFFHOST_BACKUP_ALLOW_LOCAL_DESTINATION_FOR_TESTING=true`, exactly as
  Phase 2.42's own real-infrastructure validation did -- no S3/SSH
  exercised here either).
* RPO/RTO measurement, backup-freshness monitoring, or alerting.
* Any change to the restore script's own production-target safety
  posture (Phase 2.37 §11's warning stands unchanged).
* Running this drill against staging or production targets -- not
  attempted, not safe without the CI job's own ephemeral-container
  guarantee (§3).

## 11. Testing performed

* `tests/ops/test_recovery_drill.py` -- hermetic config/artifact/
  precondition/classification tests, plus subprocess-boundary tests
  proving credential isolation, timeout handling, no destructive call
  after a failed precondition, secret redaction, and (added after an
  independent audit) fail-closed behavior on a raw, non-`DrillError`
  exception. 53 tests, all passing locally; `ruff check`/
  `ruff format --check`/`pyright` clean across the whole repository, not
  just these files.
* `tests/integration/test_recovery_drill_integration.py` -- a real,
  disposable-PostgreSQL end-to-end test (skips, rather than fails, when
  its second `DRILL_TARGET_*` instance isn't configured, so it adds one
  `skipped` to the existing `migrations-integration` job's `pytest -m
  integration` run rather than breaking it). **Not executed in this
  session**: this host has Docker but not the `psql`/`pg_dump`/
  `pg_restore` client binaries `db_backup.sh`/`db_restore.sh` invoke
  directly (they are not called through `docker exec`), so no local
  two-container run was possible here. The `recovery-drill` CI job
  installs `postgresql-client` via `apt-get`, exactly as
  `migrations-integration` already does, and is this drill's first real
  execution environment.
