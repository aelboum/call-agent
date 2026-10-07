# Phase 2.37 — PostgreSQL Backup & Recovery Foundation

Remediates the sole Phase 2.36 production blocker: PostgreSQL ran on an
unmanaged local Docker volume with no backup/restore mechanism of any kind.
This phase adds the minimal one -- standard `pg_dump`/`pg_restore`, nothing
custom -- and proves it against real disposable infrastructure: a source
database with representative multi-tenant test data, a backup, a completely
fresh target database, a restore, and verification of schema, data, foreign
keys, and Row-Level Security (including through the application's own
tenant-context mechanism, not just a superuser query).

## 1. What existed before this phase

Nothing, by design. `docs/PHASE-2.18-DEPLOYMENT-FOUNDATION.md` §7/§12
explicitly scoped PostgreSQL backup/retention/disaster-recovery out as an
*external operator responsibility* -- the `postgres_data` named volume in
`docker-compose.yml` is local, unmanaged persistence only. `docs/PHASE-0-
ARCHITECTURE.md` (G-2) separately documents that SaaS-OS ships its own
`infra/db/backup` (boto3-based), but that module is platform-team tooling an
import-linter contract forbids product code from depending on -- it is not
something this repository's own operational scripts may call into. No
existing `scripts/`, Makefile, or CI job performed a database backup before
this phase.

## 2. What this phase adds

| File | Purpose |
|---|---|
| `scripts/db_backup.sh` | `pg_dump -Fc` wrapper: fails closed (missing/empty/structurally-unreadable artifact is a non-zero exit), writes a sidecar `*.meta.json` (postgres version, timestamp, format, size, sha256, pg_dump version -- no secret values), accepts no secret via CLI argument (`PGPASSWORD` env only). |
| `scripts/db_restore.sh` | `pg_restore` wrapper: refuses a missing, empty, or structurally-corrupt archive (`pg_restore --list` check) before attempting anything; runs with `--exit-on-error` so a partial restore is a hard failure, never silent success. |
| `tests/ops/test_backup_scripts.py` | Hermetic (no PostgreSQL) static checks: strict-mode flags present, integrity checks present, no secret-bearing CLI argument or filename. |

Both scripts are plain, deterministic, dependency-free shell wrappers around
the standard PostgreSQL client tools -- no new service, no new database, no
custom archive format, no cloud/object-storage integration, matching the
phase's own minimality constraint. They live under `scripts/`, the same
"staging/ops tooling, not product code" category as the existing
`scripts/validate_staging_*.py` files -- not imported by anything under
`voiceagent/`.

## 3. Why custom format (`pg_dump -Fc`)

Chosen over plain SQL text for three properties the recovery contract
needs: `pg_restore --list` can inspect/verify archive structure *before* any
destructive restore is attempted (§6 below); the format is already
compressed; and `pg_restore` can selectively restore objects if ever
needed, without re-deriving a custom serialization. Nothing invented --
both tools ship with every PostgreSQL 16 client installation.

## 4. Backup test (real, against disposable infrastructure)

Source: fresh disposable `phase237-source-pg` (`postgres:16-alpine`,
matching `docker-compose.yml`'s own pin), provisioned with the product's
real role split (`saas_os` schema owner, restricted `saas_os_app` app role
via the existing `docker/postgres-init/01-create-app-role.sh`), migrated with
`saas-os-migrate upgrade` then `alembic upgrade head` (12 product
migrations, clean). Seeded with two real tenants, each with a user, an
agent, a published agent version, a phone number, and a `CallSession` taken
through its real lifecycle (`initiated -> ringing -> answered -> in_progress
-> completed`) via the actual `voiceagent.agents.service` /
`voiceagent.calls.service` functions -- not raw SQL.

```
$ db_backup.sh --host 127.0.0.1 --port 5432 --db voiceagent --user saas_os --output-dir /tmp/backups
[backup] pg_dump -Fc saas_os@127.0.0.1:5432/voiceagent -> /tmp/backups/voiceagent-20261003T090039Z.dump
[backup] OK: /tmp/backups/voiceagent-20261003T090039Z.dump (224890 bytes, sha256 5a85fa1e6f551ed8481a0497052d8b32769e16cef0fa2ad4c106d4b0ef370ac1)
[backup] metadata: /tmp/backups/voiceagent-20261003T090039Z.dump.meta.json
```

Metadata (`*.meta.json`): `postgres_server_version: 16.15`, `pg_dump_version:
pg_dump (PostgreSQL) 16.15`, `format: custom (pg_dump -Fc)`, `size_bytes:
224890`, `sha256: 5a85fa1e...`. Checksum re-verified identical after copying
the artifact out of the container, confirming bit-for-bit integrity of the
transfer, not just the dump.

## 5. Restore test (real, into a completely fresh/empty database)

Target: a second, separate disposable `phase237-restore-pg` -- confirmed
empty (`\dn` showed only the default `public` schema, no `app`/`core`
schema, before restoring) -- i.e. a genuine
`backup -> restore -> existing schema + existing data` exercise, not
`empty database -> migrations -> empty schema`. No migration was ever run
against this target; every object came from the archive.

```
$ db_restore.sh --host 127.0.0.1 --port 5432 --db voiceagent --user saas_os --input voiceagent-20261003T090039Z.dump
...
pg_restore: creating ACL "core.TABLE tenants"
...
[restore] OK: voiceagent-20261003T090039Z.dump -> voiceagent
```

Exit 0. Zero `pg_restore: error` lines. `--exit-on-error` was in effect the
whole run, so this is a hard guarantee, not an absence of noticed failures.

## 6. Schema verification (source vs. restored)

| Check | Source | Restored |
|---|---|---|
| Tables (`app`/`core`/`control_plane`/`self_learning`) | 48 | 48 |
| RLS enabled **and** forced (`app` schema) | 15 | 15 |
| RLS policies (`app` schema) | 15 | 15 |

Full functional equivalence, as the spec requires -- not byte-identity (ACL
restore order, OIDs, etc. legitimately differ and were not compared).

## 7. Data verification

| Table | Pre-backup (source) | Post-restore |
|---|---|---|
| `core.tenants` | 6 | 6 |
| `app.agents` | 6 | 6 |
| `app.agent_versions` | 6 | 6 |
| `app.call_sessions` | 5 | 5 |
| `app.phone_numbers` | 5 | 5 |
| `app.call_sessions` where `status='completed'` | 2 | 2 |

(Counts above 2 reflect earlier harness-script iterations' own partial rows
left in this disposable database during this phase's own development --
not a discrepancy between backup and restore, which match exactly in every
row. The two deliberately-tracked, fully-completed test records are
identified below.)

The two tracked `CallSession` records (tenant A `8f589bc2-...`, tenant B
`350af624-...`) exist post-restore with identical `status`, `end_reason`,
and `fs_channel_uuid` values, and their foreign-key chain
(`call_session -> agent -> agent_version -> phone_number`) resolves
end-to-end post-restore with every `tenant_id` along that chain identical
to the call's own -- the composite-FK cross-tenant invariant (ADR-0004)
survived the restore intact.

`alembic current` against the restored database reports `0012_inbound_
routing (head)` -- the same head as the source -- confirming the restore
correctly carried `alembic_version`, not merely the product tables.

## 8. RLS / tenant isolation verification

Performed through the **real application tenant-context mechanism**
(`voiceagent.db.tenant_session_scope`, connected as the restricted
`saas_os_app` role against the *restored* database), per the spec's own
requirement that a superuser-bypass check alone is insufficient:

```
A_sees_own_call = True
A_sees_Bs_call = False
A_agent_count = 1
B_sees_own_call = True
B_sees_As_call = False
B_agent_count = 1

ALL RLS ISOLATION ASSERTIONS PASSED (via real application tenant_session_scope, not superuser bypass)
```

All four isolation assertions hold (tenant A reads its own data, tenant A
is denied tenant B's, and symmetrically) -- RLS and FORCE RLS, restored from
the archive, are enforcing isolation for a real non-superuser connection,
not merely present in the schema.

`tests/integration/test_domain_rls_integration.py -m integration` (19 tests,
the pre-existing Phase 2.1 RLS suite) was additionally run against the
*source* database and passed in full -- confirming the backup/restore work
this phase added did not disturb the platform's own, already-validated RLS
behavior.

## 9. Application-level recovery verification

Connected the real `voiceagent.db` seam (not raw psycopg) to the restored
database via `DATABASE_URL`/`MIGRATIONS_DATABASE_URL` as the restricted
`saas_os_app` role: `tenant_session_scope` queries succeeded (§8), `alembic
current` recognized the schema as current (§7), and no schema-mismatch
error occurred anywhere in this process. No real external (telephony/AI
provider) call was made -- out of scope for this phase by design.

## 10. Failure-case tests

Both run against disposable copies; the one valid backup artifact was never
touched:

```
$ db_restore.sh ... --input /tmp/backups/does-not-exist.dump
ERROR: backup artifact not found: /tmp/backups/does-not-exist.dump   (exit 1)

$ head -c 5000 voiceagent-...dump > corrupted.dump   # disposable truncated copy
$ db_restore.sh ... --input corrupted.dump
ERROR: backup artifact failed structural integrity check (pg_restore --list) -- refusing to restore a corrupt archive: corrupted.dump   (exit 1)
```

Both fail loudly and immediately, before any destructive `pg_restore`
invocation runs -- not a silent "success" against bad input.

## 11. Operational procedure

### Backup

Prerequisites: network access to the source PostgreSQL as its
schema-owning role (not the restricted application role -- ownership and
RLS policy definitions must be captured); `PGPASSWORD` set in the
environment (never passed as a CLI argument); an `--output-dir` **outside**
this repository and outside any Docker build context (an operator-managed
backup location/volume -- never commit the artifact).

```bash
export PGPASSWORD=<schema-owner password, from infra.secrets, never hardcoded>
scripts/db_backup.sh --host <host> --port <port> --db voiceagent \
    --user saas_os --output-dir /path/outside/this/repo
```

Output: `<output-dir>/voiceagent-<UTC timestamp>.dump` plus a
`.meta.json` sidecar. Verify the printed `sha256` against the sidecar
before considering the backup durable/transferred.

### Restore

Prerequisites: a target PostgreSQL instance/database that already has the
same role structure as the source (run the identical role-provisioning step
`docker/postgres-init/01-create-app-role.sh` already runs for a fresh
deployment) and is otherwise **empty of this product's own schema**.

```bash
export PGPASSWORD=<target schema-owner password>
scripts/db_restore.sh --host <host> --port <port> --db voiceagent \
    --user saas_os --input /path/to/voiceagent-<timestamp>.dump
```

After restoring: run `alembic current` and confirm it reports the expected
head; spot-check a known tenant/record; reconnect the application
(`DATABASE_URL`/`MIGRATIONS_DATABASE_URL`) and confirm `tenant_session_scope`
queries succeed for a real tenant.

### Warning

**Restoring over a database that already holds real data is destructive**:
`pg_restore` creates objects on top of whatever is already there, and a
name collision is an error at best and silent corruption at worst if object
identity happens to line up. Neither script contains any production-target
safety check -- that is deliberate; this phase proves the mechanism works,
it does not authorize an unreviewed destructive command against a live
database. An actual production restore requires its own separate, reviewed,
explicit operational procedure (maintenance window, stakeholder sign-off,
a freshly-provisioned or explicitly-emptied target) -- not merely running
these two scripts unattended.

## 12. Storage / retention

This phase proves the **mechanism**, not an off-host storage story. Three
distinct things, not to be confused:

* **Database volume** (`postgres_data` in `docker-compose.yml`) -- local,
  unmanaged container persistence. Not a backup.
* **Backup artifact** (`*.dump` produced by `db_backup.sh`) -- a point-in-
  time snapshot. What this phase adds and proves.
* **Off-host backup storage** -- does not exist yet. No cloud/object-storage
  integration was added (explicitly out of scope, §2 of the brief); an
  operator running `db_backup.sh` today still has to move the artifact
  somewhere durable themselves. This is a real, intentional gap, not an
  oversight -- the next operational phase's natural subject.

No retention policy, no scheduling, and no automation of either exists.
Running `db_backup.sh` is, today, a manual, operator-initiated action.

## 13. Security

* No actual database dump or test backup artifact was committed --
  `git status --short` (checked throughout, see final report §A) shows
  only the two new scripts, the new test file, and this document as new
  untracked paths; no `.dump`/`.sql` file anywhere in the working tree.
* All backup/restore work in this phase wrote artifacts to a path outside
  this repository (`%TEMP%`-rooted, never under `ai-agent/`) specifically
  so nothing could be accidentally picked up by a Docker build context or
  `git add -A`.
* `PGPASSWORD` is read from the environment only; neither script accepts a
  password as a CLI argument (visible in `ps`/process listings) or embeds
  one in a filename. No password or connection string containing one was
  printed to any log captured in this document.
* No `.gitignore` change was needed -- no backup-artifact path exists
  inside the repository for anything to ignore.

## 14. Remaining limitations

* **Local backup only** -- no off-host/durable storage integration (§12).
* **No retention policy or scheduling** -- entirely manual today.
* **No point-in-time recovery (PITR)** -- `pg_dump`/`pg_restore` only;
  WAL archiving/continuous recovery is explicitly out of this phase's
  scope and remains unimplemented.
* **Not validated at production scale** -- this proof used a small,
  disposable, single-digit-row dataset. Restore duration, lock behavior,
  and resource use against a multi-GB production-sized database were not
  exercised and should not be assumed from this result.
* **No production restore validation** -- by design (§15 of the brief):
  this phase proves the mechanism against disposable infrastructure only,
  never against a database holding real data.
* **No automated backup verification job** -- an operator must run
  `db_backup.sh` and check its own output; nothing currently alerts if a
  scheduled backup (once scheduling exists) silently stops happening.
