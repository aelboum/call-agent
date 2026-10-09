# Off-Host Backup: Real-Remote Acceptance Procedure

Not a phase, not new code -- a runbook for the one thing Phase 2.42
explicitly could not do in this sandbox: validate `scripts/offhost_backup.py`
against a *genuinely remote* destination. §13 of that phase's own doc named
it directly: "No real network transport was exercised ... network failure
modes specific to a real remote were not exercised."

## 1. What is actually implemented today

One transport kind: `OFFHOST_BACKUP_DESTINATION_KIND=command`. Five
operator-supplied command templates (argv arrays, never a shell string --
`subprocess.run(..., shell=False)`), read by `load_config()`
(`scripts/offhost_backup.py:140`):

| Config var | Purpose | Required? |
|---|---|---|
| `OFFHOST_BACKUP_TRANSFER_COMMAND` | copy local artifact to destination | always, when enabled |
| `OFFHOST_BACKUP_VERIFY_COMMAND` | print the remote file's sha256 | always, when enabled |
| `OFFHOST_BACKUP_LIST_COMMAND` | list artifact names at destination | optional |
| `OFFHOST_BACKUP_DELETE_COMMAND` | delete one named artifact | required only if retention enabled |
| `OFFHOST_BACKUP_FETCH_COMMAND` | copy a remote artifact back locally | required only for `recover` |

There is no S3 SDK, no SSH library, no cloud-provider-specific code
anywhere in this module -- "S3" and "SSH" below are two concrete
instantiations an operator plugs in via these five templates, not two
different code paths this product maintains.

## 2. What exists in this sandbox right now

Checked: no `AWS_*`/`S3_*`/`SSH_*`/`OFFHOST_*`/`RCLONE_*` environment
variable is set anywhere in this environment. No real bucket, no real SSH
host, no credential of any kind for either. **A locally spun-up MinIO or
SSH container would not close this gap** -- it would just re-run Phase
2.42's own already-completed local "command" transport test
(`OFFHOST_BACKUP_ALLOW_LOCAL_DESTINATION_FOR_TESTING=true`, cp/sha256sum)
under a different name. The entire point of this gap is *real* network
behavior (auth expiry, partial writes on a flaky connection, latency,
real IAM/key-based auth failures) -- something genuinely remote and
non-production-shared is the only thing that proves it, and none exists
here. **Status: blocked on missing infrastructure, not missing code.**

## 3. Minimum access required to clear this

One of:

* A real S3-compatible bucket (AWS S3, or any S3-compatible provider) with
  a scoped IAM credential: `s3:PutObject`/`s3:GetObject`/`s3:ListBucket`/
  `s3:DeleteObject`, restricted to one prefix, on a bucket that is not
  shared with production data.
* A real SSH host reachable from wherever this validation runs, with a
  dedicated key and a directory this product may freely write to and
  delete from.

Either is non-production, bounded-scope infrastructure -- not a "paid
service" purchase in the sense the mandate restricts (most providers offer
a free tier adequate for a few-KB test dump), but still something this
session cannot provision for itself without the account owner's action.

## 4. The acceptance procedure, ready to run the moment #3 exists

Six scenarios, in order, each against the same real destination. All
commands assume `OFFHOST_BACKUP_ENABLED=true`,
`OFFHOST_BACKUP_DESTINATION_KIND=command`, `OFFHOST_BACKUP_STATE_DIR` set to
a scratch directory, and `OFFHOST_BACKUP_ALLOW_LOCAL_DESTINATION_FOR_TESTING`
**left unset** (a `<scheme>://` root is what makes this a real-remote run
at all, per §2.2 of Phase 2.42's own doc).

### 4.1 Upload (S3 example; SSH is the same shape with `scp`/`ssh`)

```
export OFFHOST_BACKUP_DESTINATION_ROOT=s3://<bucket>/<test-prefix>
export OFFHOST_BACKUP_TRANSFER_COMMAND='["aws","s3","cp","{LOCAL_PATH}","s3://<bucket>/<test-prefix>/{REMOTE_NAME}"]'
export OFFHOST_BACKUP_VERIFY_COMMAND='["bash","-c","aws s3api head-object --bucket <bucket> --key <test-prefix>/{REMOTE_NAME} --query ChecksumSHA256 --output text || aws s3 cp s3://<bucket>/<test-prefix>/{REMOTE_NAME} - | sha256sum"]'
python scripts/offhost_backup.py backup --host <pg-host> --port 5432 --db voiceagent --user saas_os --output-dir /tmp/real-remote-backup
```
**Pass**: exit 0, a ledger entry appended (`cat $OFFHOST_BACKUP_STATE_DIR/offhost_backup_ledger.json`), the object visible in the bucket/host independently of this tool (`aws s3 ls`/an `ssh` directory listing).

### 4.2 Remote verification catches a real mismatch

Manually corrupt the uploaded object (overwrite one byte via the
provider's own console/CLI, independent of this tool), then re-run
`freshness`/`recover` or re-trigger `off_host_verify()` via a fresh
`backup` call against a new dump. **Pass**: the sha256 comparison fails
closed, no ledger entry is written, the local verified artifact is still
retained (`db_backup.sh`'s own output directory, untouched).

### 4.3 Retrieval

```
python scripts/offhost_backup.py recover --output-dir /tmp/real-remote-recover
```
**Pass**: exit 0, the fetched file's sha256 matches the ledger's recorded
value (the command itself asserts this; also confirm by hand with
`sha256sum`).

### 4.4 Checksum/integrity failure injection

Same as §4.2 but via `fetch`: corrupt the remote object after a successful
upload, then run `recover`. **Pass**: `OffHostError`, non-zero exit, the
corrupted file is not left in place as if it were good (check the output
directory is either empty or clearly marked incomplete).

### 4.5 Destination outage

Point `OFFHOST_BACKUP_TRANSFER_COMMAND`/`_VERIFY_COMMAND` at an
intentionally unreachable host/bucket (wrong region, revoked credential,
or a `ssh` target that is firewalled), run `backup`. **Pass**: non-zero
exit, `OffHostError` surfaced within the transport's 300s timeout (see §5
-- `_run_transport()` already enforces this; a hang past ~300s would
itself be the finding), no ledger entry, local artifact retained.

### 4.6 Retention against the real destination

```
export OFFHOST_BACKUP_RETENTION_ENABLED=true
export OFFHOST_BACKUP_DELETE_COMMAND='["aws","s3","rm","s3://<bucket>/<test-prefix>/{REMOTE_NAME}"]'
export OFFHOST_BACKUP_RETENTION_KEEP_COUNT=2
python scripts/offhost_backup.py retention
```
Run with at least 3 ledger entries already present (repeat §4.1 three
times with distinct dumps first). **Pass**: exactly the oldest entries
beyond `keep_count` are deleted both remotely (confirm independently) and
from the ledger; the newest entry is never deleted even if a policy would
otherwise select it (`select_retention()`'s own invariant, already proven
hermetically in `tests/ops/test_offhost_backup.py`).

## 5. What to watch for and record, not assume

* **Transport timeout is 300s, enforced by us, not the operator's command.**
  `_run_transport()` (`scripts/offhost_backup.py:318`) passes `timeout=300.0`
  to `subprocess.run()` regardless of whatever timeout behavior the
  operator's own command has (`aws s3 cp`'s own retry/timeout defaults, a
  bare `scp`'s lack thereof), and converts a `subprocess.TimeoutExpired`
  into a fail-closed `OffHostError`. §4.5 is where this is exercised; a
  hang past ~300s without that `OffHostError` would be the real finding
  to report, not an expected gap.
* Record wall-clock duration for §4.1/§4.3 against the real destination --
  Phase 2.42 never measured this because it never ran one.
* Record the exact provider/region/distance used, so a future "it was
  fast/slow" comparison has a baseline.

## 6. Non-goals

Does not test multi-GB production-scale dumps (Phase 2.42 §13 already
named this separately). Does not test concurrent backup runs against the
same remote destination from two hosts (not a scenario this product's own
locking model -- `acquire_lock()`, local-only -- claims to prevent across
hosts; worth flagging to the operator as a known multi-host gap rather
than silently assuming the local lock covers it).
