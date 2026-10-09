# Phase 2.42 — Off-Host Backup Transfer & Retention

Closes the two gaps Phase 2.37 named and deliberately left open (its own
doc, §12/§14): backups were local-only, with no off-host copy and no
retention policy. This phase adds both, without touching the Phase 2.37
mechanism itself.

**PostgreSQL backup success means the backup is locally verified AND
off-host verified.** A transfer failure or a remote checksum mismatch is
always a failed operation -- never a degraded success -- and the local
verified artifact is always retained until an off-host copy is
independently proven good.

## 1. What this phase does NOT change

* `scripts/db_backup.sh` and `scripts/db_restore.sh` (Phase 2.37) are
  unmodified. `pg_dump`/`pg_restore` are still the only things that ever
  touch PostgreSQL for backup/restore purposes.
* Redis is not, and has never been, a backup source. Nothing in this phase
  reads from or writes to Redis.
* The SaaS-OS pin (`ff550010e5eafecace7311038aadc99fcecfbe3d`) is untouched.
  SaaS-OS itself is never modified.
* No cloud SDK (boto3, azure-storage, google-cloud-storage, etc.) was added
  as a dependency.

## 2. Architecture

```
PostgreSQL --pg_dump--> local verified .dump (db_backup.sh, unchanged)
    |
    v
scripts/offhost_backup.py:
    build manifest (non-secret metadata) + detached sha256
    |
    v
transfer (operator-configured command) ------> off-host destination
    |
    v
remote existence check + remote sha256 --compare--> local sha256
    |
    v (only on match)
record in local ledger of VERIFIED backups
    |
    v
retention (deterministic, ledger-driven) --delete--> expired off-host artifacts
```

### 2.1 The transport: a generic command boundary, not a cloud SDK

`OFFHOST_BACKUP_DESTINATION_KIND=command` is the only supported transport.
The operator supplies four (plus one optional) command *templates* as JSON
arrays of argv tokens -- never a shell string:

| Config var | Purpose | Token placeholders |
|---|---|---|
| `OFFHOST_BACKUP_TRANSFER_COMMAND` | copy one local file to the destination | `{LOCAL_PATH}`, `{REMOTE_NAME}`, `{REMOTE_ROOT}` |
| `OFFHOST_BACKUP_VERIFY_COMMAND` | print the remote file's sha256 | `{REMOTE_NAME}`, `{REMOTE_ROOT}` |
| `OFFHOST_BACKUP_LIST_COMMAND` (optional) | list artifact names at the destination | `{REMOTE_ROOT}` |
| `OFFHOST_BACKUP_DELETE_COMMAND` | delete one named artifact (retention only) | `{REMOTE_NAME}`, `{REMOTE_ROOT}` |
| `OFFHOST_BACKUP_FETCH_COMMAND` (optional) | copy a remote artifact back to a local path (`recover` only) | `{REMOTE_NAME}`, `{REMOTE_ROOT}`, `{LOCAL_PATH}` |

Each is executed as an argument array (`subprocess.run(..., shell=False)`),
never interpolated into a shell string. This means an S3-compatible store
(`aws s3 cp`/`rclone`), SSH/SFTP (`scp`/`rsync -e ssh`), or a mounted
object-storage filesystem can all be plugged in without changing anything
about how a backup is produced, verified, or retained -- the destination is
opaque to this script. Nothing about the dump format, the manifest format,
or the retention logic depends on which transport is configured.

### 2.2 Local vs. genuinely off-host

`OFFHOST_BACKUP_DESTINATION_ROOT` must contain a `<scheme>://` (e.g.
`s3://bucket/prefix`, `ssh://host/path`) -- a plain local filesystem path is
rejected with a clear configuration error unless
`OFFHOST_BACKUP_ALLOW_LOCAL_DESTINATION_FOR_TESTING=true` is also set. That
flag exists for integration testing only (see §8) and must never be set in
production; its name says so. The destination root is additionally rejected
if it looks like a web-served directory (`/var/www`, `wwwroot`, etc.) --
best-effort protection against artifacts becoming publicly readable through
an accidentally configured web root.

## 3. Backup lifecycle (`scripts/offhost_backup.py backup`)

1. Acquire the cross-process lock (§6).
2. Run `db_backup.sh` unchanged (subprocess, argument array, `PGPASSWORD`
   flows to this one subprocess only).
3. Build the manifest (§4) and write it plus its detached checksum
   alongside the dump; `chmod 600` all four local files.
4. Transfer all four files to the destination.
5. Verify the dump exists remotely, compute its remote sha256, compare to
   the local one. Any failure here aborts the operation: the local verified
   artifact is retained, nothing is recorded in the ledger, and retention
   never runs.
6. On success, append a ledger entry (§5) -- this is the ONLY thing that
   marks a backup "off-host verified".
7. If retention is enabled, run it (§5).

If `OFFHOST_BACKUP_ENABLED=false` (the default), steps 3-7 are skipped
entirely and the script behaves exactly like a thin wrapper around
`db_backup.sh` -- Phase 2.37's own local-only behavior, unchanged.

## 4. Manifest

A sidecar `<dump>.manifest.json` next to the dump, containing only:
`backup_format_version`, `backup_id`, `created_at_utc`, `database`,
`dump_filename`, `sha256`, `size_bytes`, `format`, `migration_revision`
(operator-supplied, optional, non-secret -- e.g. an Alembic revision id).

Never contains a password, connection string, token, or key.
`assert_manifest_has_no_secrets()` is a defense-in-depth check that runs
before every write: it rejects any field whose *name* looks secret-bearing,
and any string value that looks like a connection string with embedded
credentials -- even though every field currently written is built from
known-safe sources, a future accidental addition fails closed instead of
silently shipping a credential off-host.

The manifest is bound to its own content with a detached checksum file,
`<manifest>.sha256`, in standard `sha256sum -c`-compatible format.

## 5. Ledger and retention

### 5.1 Ledger

`OFFHOST_BACKUP_STATE_DIR/offhost_backup_ledger.json` is a local, `chmod
600` JSON array. An entry is appended *only* after off-host verification
has already succeeded (§3 step 6) -- so an entry's mere presence in the
ledger is proof of verification. There is no separate "verified: true/false"
field to get out of sync with reality: an unverified or failed backup
simply never appears.

This also means retention (below) can never be tricked into deleting an
artifact whose verification state is unknown -- unverified backups are not
merely skipped by a check, they are structurally absent from the one list
retention ever reads.

### 5.2 Retention policy

One policy, configurable along two independent axes (either or both):

* `OFFHOST_BACKUP_RETENTION_KEEP_COUNT=N` -- keep the N most recent verified
  backups.
* `OFFHOST_BACKUP_RETENTION_KEEP_DAYS=D` -- keep verified backups newer than
  D days.

A backup survives if it satisfies *either* configured axis (union, not
intersection) -- `select_retention()` in `scripts/offhost_backup.py`,
pure logic, unit-tested in isolation from any transport.

Safety guarantees, all enforced in that same pure function:

* Ordering is by `backup_id` (which embeds the backup's own UTC timestamp),
  never filesystem mtime.
* The single newest verified backup always survives, unconditionally --
  retention can never empty the ledger, regardless of configured policy.
* Only entries already in the ledger (i.e. already off-host verified) are
  ever candidates for deletion.
* A deletion failure for one backup's artifacts does not stop retention
  from proceeding for others, and that backup's ledger entry is NOT
  removed -- it stays "verified" and is retried on the next retention run,
  and the overall operation's exit code reflects the failure (§7).

Retention can run inline as part of `backup` (if
`OFFHOST_BACKUP_RETENTION_ENABLED=true`) or standalone via
`scripts/offhost_backup.py retention` -- idempotent, safe to run on its own
schedule.

## 6. Locking

A lock file (`OFFHOST_BACKUP_STATE_DIR/offhost_backup.lock`) holding
`{pid, token, started_at_utc}`, created with `O_CREAT|O_EXCL` for atomicity.
Wraps the entire `backup`/`retention` operation, not just the transfer step
-- two concurrent `backup` invocations cannot even both run `pg_dump`.

A second concurrent invocation fails immediately
(`"another backup/retention operation is already running"`, exit 1) rather
than racing. On acquire, an existing lock is treated as stale -- removed and
retried once -- if its recorded PID is no longer alive OR it is older than
`OFFHOST_BACKUP_LOCK_STALE_SECONDS` (default 3600). A lock is only ever
released by the holder that wrote it (checked by its own random `token`,
not just PID) -- so a process that recovered a stale lock and is itself
later mistaken for stale cannot have its own fresh lock deleted out from
under it by the original, now-exiting holder.

## 7. Failure semantics

| Failure | Local artifact | Ledger | Exit code |
|---|---|---|---|
| Config invalid | n/a -- nothing ran | untouched | 2 |
| `db_backup.sh` fails | none produced | untouched | 1 |
| Transfer fails | retained | no entry added | 1 |
| Remote checksum mismatch | retained | no entry added | 1 |
| Retention delete fails for an entry | n/a | entry stays (retried later) | 1 |
| Lock already held | n/a -- nothing ran | untouched | 1 |

A non-zero exit from `scripts/offhost_backup.py backup` is always
actionable: stderr names exactly which stage failed and (for a transfer or
verification failure) explicitly states that the local artifact was
retained.

## 8. Security

* **Credentials.** `PGPASSWORD`/`DATABASE_URL`/`MIGRATIONS_DATABASE_URL`
  reach exactly one subprocess: `db_backup.sh`/`db_restore.sh` themselves
  (via `os.environ.copy()`, matching Phase 2.37's own convention). Every
  transport subprocess (transfer/verify/list/delete/fetch) instead runs
  under `_transport_env()`, a minimal environment built from scratch
  (`PATH`/`HOME`/`LANG`/a few platform variables) plus only variables an
  operator explicitly names in `OFFHOST_TRANSPORT_ENV_PASSTHROUGH` -- and
  `PGPASSWORD`/`DATABASE_URL`/`MIGRATIONS_DATABASE_URL` can never be added
  to that passthrough list; `load_config()` rejects it at startup
  (`test_config_denylist_cannot_be_passed_through_to_transport`).
* **Command execution.** Every subprocess call (`db_backup.sh` invocation
  and every transport command) is an argument array, never a shell string
  (`shell=True` is never used anywhere in this file).
* **Injection.** Every configured string (destination root, every token of
  every command template) is rejected at config-load time if it contains a
  control character or newline (`_no_control_chars`) -- an operator-supplied
  path or command cannot smuggle a second command via `\n` injection even
  though no shell is involved.
* **Permissions.** The dump, its `.meta.json`, the manifest, the manifest's
  checksum, and the ledger are all `chmod 600` immediately after being
  written (`os.chmod(path, 0o600)`), verified in the real-infrastructure run
  (§9) via `stat -c %a` inside the container, not just asserted.
* **Web-root accident.** `OFFHOST_BACKUP_DESTINATION_ROOT` is rejected if it
  contains a common web-served-directory hint (`/var/www`, `wwwroot`,
  `htdocs`, `nginx/html`, `/static/`) -- best-effort, not exhaustive, since
  a generic command transport can ultimately point anywhere.
* **Manifest.** See §4 -- `assert_manifest_has_no_secrets()` runs before
  every write.
* **detect-secrets.** `detect-secrets scan --baseline .secrets.baseline` was
  run against the full repository including both new files; it flagged
  nothing in either. (Its own baseline-refresh side effect on pre-existing,
  already-allowlisted entries -- a Windows path-separator artifact unrelated
  to this phase -- was reverted rather than committed; see the audit
  report's Git State section.)

## 9. Real-infrastructure validation performed

Two disposable `postgres:16-alpine` containers (`phase242-pg`,
`phase242-pg-restore`) plus a `postgres:16-alpine`-based runner (same image,
so `pg_dump`/`pg_restore`/`psql` are version-matched, with `python3`/`bash`
added via `apk`) on an isolated Docker network, all removed at the end of
the run. The runner had the real repository bind-mounted, plus three
separate host directories acting as the local backup output, the
"off-host" destination, and the lock/ledger state dir.

**Transport used for this validation**: the generic command transport
configured with real `cp`/`sha256sum`/`ls`/`rm` -- genuine subprocesses,
not a mock standing in for "remote" -- pointed at a directory that is
physically separate from the local backup output directory, with
`OFFHOST_BACKUP_ALLOW_LOCAL_DESTINATION_FOR_TESTING=true` (test-only, as
documented in §2.2). No real network transport (S3/SSH) was available in
this environment -- see Limitations.

* **Backup**: `pg_dump -Fc` against `phase242-pg` (seeded with a
  representative two-tenant `app.tenants`/`app.agents` schema and FK),
  transferred, remote-checksum-verified, manifest written and bound,
  ledger entry recorded. `stat -c %a` inside the container confirmed `600`
  on the dump, `.meta.json`, manifest, and manifest checksum files.
* **Recovery**: `scripts/offhost_backup.py recover` fetched the artifact
  back and checksum-verified it; the unmodified `scripts/db_restore.sh`
  then restored it into the completely fresh, confirmed-empty
  `phase242-pg-restore`. Post-restore: `app.tenants`/`app.agents` row
  counts matched the source (2/2) and the `agents.tenant_id` FK chain
  resolved correctly for both rows.
* **Corruption**: the off-host copy was corrupted in place (bytes
  appended). `recover` detected the sha256 mismatch, refused to hand the
  artifact to restore, exited non-zero, and left the local verified copy
  and the ledger untouched.
* **Retention**: three verified backups were produced; `retention` with
  `OFFHOST_BACKUP_RETENTION_KEEP_COUNT=2` deleted exactly the oldest
  (all four of its off-host sidecar files) and kept exactly the two newest,
  matching `select_retention()`'s own deterministic ordering.
* **Interrupted transfer**: a backup run against a destination root that
  does not exist failed at the transfer step (real `cp` error, not
  simulated); the local artifact was retained, no ledger entry was added,
  retention did not run, and the two prior good off-host backups were
  verified untouched. A subsequent normal backup immediately succeeded.
* **Concurrency**: two `backup` invocations launched as separate OS
  processes (via `docker exec`) at nearly the same instant against the
  same state directory -- one ran to completion, the other failed
  immediately with the lock-held error and exit 1. Neither corrupted the
  other's artifact or the ledger.

Hermetic stale-lock recovery (dead PID, and separately, an old timestamp
with a live PID) is covered by `tests/ops/test_offhost_backup.py` rather
than repeated against real infrastructure, since it is pure
filesystem/PID-table logic with no PostgreSQL or transport dependency.

## 10. Restore procedure (unchanged from Phase 2.37, plus recovery)

```bash
# 1. Pull an off-host artifact back to a local path and verify its checksum:
python scripts/offhost_backup.py recover --output-dir /path/outside/this/repo \
    [--backup-id voiceagent-<timestamp>]   # defaults to the newest verified backup

# 2. Hand it to the unmodified Phase 2.37 restore script:
export PGPASSWORD=<target schema-owner password>
scripts/db_restore.sh --host <host> --port <port> --db voiceagent \
    --user saas_os --input /path/outside/this/repo/voiceagent-<timestamp>.dump
```

Every Phase 2.37 restore warning still applies unchanged: the target must
already have the product's role structure and be empty of this product's
own schema; restoring over a database holding real data is destructive and
is not what either script protects against.

## 11. Scheduling recommendation

No scheduler was introduced -- `scripts/offhost_backup.py backup` is a
single idempotent command suitable for cron/systemd-timer/a container's own
scheduled job. Recommended production schedule: one `backup` invocation per
desired recovery-point interval (e.g. daily), with
`OFFHOST_BACKUP_RETENTION_ENABLED=true` so retention runs inline after every
successful backup rather than needing a second scheduled job. The lock
(§6) makes a missed/late cron tick safe -- an overlapping run exits cleanly
rather than corrupting anything.

## 12. Operational commands

```bash
# Backup (+ off-host transfer + retention, if configured):
python scripts/offhost_backup.py backup --host <h> --port <p> --db voiceagent \
    --user saas_os --output-dir <local-dir> [--migration-revision <rev>]

# Retention only (idempotent, safe on its own schedule):
python scripts/offhost_backup.py retention

# Recover an off-host artifact locally (then feed it to db_restore.sh):
python scripts/offhost_backup.py recover --output-dir <local-dir> [--backup-id <id>]
```

Full configuration reference (environment variables):
`OFFHOST_BACKUP_ENABLED`, `OFFHOST_BACKUP_DESTINATION_KIND` (must be
`command`), `OFFHOST_BACKUP_DESTINATION_ROOT`, `OFFHOST_BACKUP_TRANSFER_COMMAND`,
`OFFHOST_BACKUP_VERIFY_COMMAND`, `OFFHOST_BACKUP_LIST_COMMAND` (optional),
`OFFHOST_BACKUP_FETCH_COMMAND` (optional, needed for `recover`),
`OFFHOST_BACKUP_STATE_DIR`, `OFFHOST_BACKUP_RETENTION_ENABLED`,
`OFFHOST_BACKUP_DELETE_COMMAND`, `OFFHOST_BACKUP_RETENTION_KEEP_COUNT`,
`OFFHOST_BACKUP_RETENTION_KEEP_DAYS`, `OFFHOST_BACKUP_LOCK_STALE_SECONDS`
(default 3600), `OFFHOST_TRANSPORT_ENV_PASSTHROUGH`,
`OFFHOST_BACKUP_ALLOW_LOCAL_DESTINATION_FOR_TESTING` (test-only, never
production).

## 13. Limitations

* **No real network transport was exercised.** Validation (§9) used the
  generic command transport with real local `cp`/`sha256sum`/`ls`/`rm`
  against a separate directory, not a real S3-compatible store or SSH
  host -- the code path is identical (the abstraction has no
  network-vs-local special case), but network failure modes specific to a
  real remote (partial writes over a flaky connection, auth expiry,
  latency/timeouts under load) were not exercised. An operator adopting
  this must validate their *own* configured transport command against
  their real destination before relying on it.
* **The ledger is the only record of "which backups are off-host
  verified," and it is local.** If `OFFHOST_BACKUP_STATE_DIR` is lost, the
  verified-backup history is lost even though the off-host artifacts
  themselves may still exist -- retention and `recover --backup-id
  <newest>` would no longer know about them (an explicit `--backup-id`
  still works if the id is known from elsewhere, e.g. a manifest filename
  observed directly at the destination). The state directory should be
  backed up or placed on durable storage itself.
* **Off-host backup does not by itself provide PITR.** Still `pg_dump`
  snapshots at discrete points in time, same as Phase 2.37 -- no WAL
  archiving or continuous recovery.
* **Backup retention is not a substitute for restore drills.** This phase
  proves the mechanism (§9) against disposable infrastructure; it does not
  establish an ongoing production restore-drill practice.
* **Redis is not authoritative backup state and was never consulted** by
  any part of this phase.
* **Local backup alone remains insufficient for production DR** -- this is
  exactly the gap this phase closes when `OFFHOST_BACKUP_ENABLED=true` is
  actually configured with a real destination; with it left at its default
  `false`, an operator is back to Phase 2.37's own local-only posture.
* **Not validated at production scale** -- the real-infrastructure run
  (§9) used a small, few-row representative dataset, consistent with
  Phase 2.37's own stated scope; transfer duration/retry behavior against a
  multi-GB production dump was not exercised.
* **No alerting.** A failed scheduled backup/retention run must currently
  be noticed from the scheduler's own job-failure signal (cron mail,
  systemd-timer failure state, etc.) -- nothing in this phase pages anyone.
