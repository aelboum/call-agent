# Phase 2.44 — Backup Freshness Monitoring

Closes the gap Phase 2.42 named and deliberately left open (its own doc,
§13): "No alerting. A failed scheduled backup/retention run must currently
be noticed from the scheduler's own job-failure signal ... nothing in this
phase pages anyone." This phase gives an operator a way to detect that
failure independently of trusting the scheduler's own per-run exit code --
in particular, it also catches the case a job-failure signal cannot: the
scheduler silently stopped firing at all.

## 1. What this phase does NOT change

* `scripts/db_backup.sh`, `scripts/db_restore.sh` (Phase 2.37) and
  `scripts/db_recovery_drill.py` (Phase 2.43) -- untouched.
* `scripts/offhost_backup.py`'s own `backup`/`retention`/`recover`
  subcommands, the ledger format (`offhost_backup_ledger.json`), and
  `record_verified()`'s own "an entry's mere presence in the ledger is
  proof of off-host verification" contract (Phase 2.42 §9) -- unchanged.
  This phase only *reads* the ledger; nothing it adds ever calls
  `save_ledger()`.
* No scheduler, cron job, systemd timer, or new infrastructure dependency
  was introduced -- same restraint Phase 2.43 §10 already applied to the
  recovery drill. An operator wires the new subcommand into whatever
  monitoring/paging system they already have; this phase supplies the
  signal, not the pager.
* No cloud SDK, metrics backend, or new dependency was added.

## 2. What this phase adds

A new `freshness` subcommand on the existing `scripts/offhost_backup.py`
CLI (not a new script -- it reuses `load_config()` and `load_ledger()`
unchanged, rather than duplicating config-loading or ledger-reading logic):

```
python scripts/offhost_backup.py freshness --max-age-hours 26
```

`check_freshness(ledger, max_age_hours, now)` is pure logic over the
already-loaded ledger: it finds the newest entry by `verified_at_utc`, and
reports `FAIL` if the ledger is empty (no backup has ever been off-host
verified -- strictly worse than merely stale) or if the newest entry's age
exceeds `--max-age-hours`, otherwise `PASS`. There is no built-in default
threshold -- an operator must state their own actual backup cadence
explicitly (fail-closed: a silently-assumed default could be wrong for a
given deployment's real schedule).

`_cmd_freshness()` requires `OFFHOST_BACKUP_ENABLED=true` (the same
precondition `recover`/`retention` already require) and exits `2` on
misconfiguration, `1` on a `FAIL` verdict, `0` on `PASS` -- distinct exit
codes an operator's monitoring can branch on directly (`2` = "check your
own config", `1` = "backups are actually stale"). The command prints one
JSON object to stdout on every path (`{"verdict", "reason",
"backups_in_ledger", "newest_backup_id", "newest_verified_at_utc",
"age_hours", "max_age_hours"}`), never to a file -- an operator's own cron
wrapper, healthcheck endpoint, or Prometheus textfile-collector script
reads stdout and exit code; this phase does not choose how an operator
pipes either of those onward.

No secret, password, or connection string ever appears in the ledger (Phase
2.42 §9's own `record_verified()` writes only `backup_id`/timestamps/
filenames/`sha256`) or in this command's output -- there is nothing to
redact because nothing secret is ever read.

## 3. Failure behavior

* Off-host disabled (`OFFHOST_BACKUP_ENABLED` unset/false) -- exit `2`,
  stderr only, no JSON (there is no ledger to report on).
* Ledger file missing or empty -- `FAIL`, `backups_in_ledger: 0`.
* Newest verified backup older than `--max-age-hours` -- `FAIL`, with the
  computed age and threshold in `reason`.
* Otherwise -- `PASS`.

No retry, no fallback, no second ledger location -- the same single-
mechanism posture the rest of this file already follows.

## 4. Non-goals

* No scheduler/cron/systemd-timer integration (see §1).
* No alerting/paging transport (email, Slack, PagerDuty, etc.) -- out of
  scope; this phase supplies a checkable signal (exit code + JSON), not a
  notification channel.
* No metrics-backend export (OpenTelemetry, Prometheus client library,
  etc.) -- `voiceagent/metrics.py`'s OTel instruments are for the
  long-lived call-runtime/API/worker processes; `offhost_backup.py` is a
  short-lived CLI invocation with no running `MeterProvider` to export
  through, so stdout JSON is the simplest correct interface, consistent
  with how `backup`/`retention`/`recover` already report (plain stdout
  lines, no metrics instrumentation).
* Does not validate that the *backup content* is restorable -- that is
  Phase 2.43's job (recovery drills), run on a wholly different cadence
  (CI, on every push/PR) from this phase's concern (wall-clock staleness
  of the off-host-verified ledger in a real, running deployment).

## 5. Testing performed

`tests/ops/test_offhost_backup.py` -- `check_freshness()` pure-logic cases
(empty ledger, within threshold, over threshold, newest-by-timestamp not
by list order) and `_cmd_freshness()` CLI cases (misconfiguration exit code,
stale-ledger JSON output and exit code). All hermetic, no PostgreSQL or
real transport needed -- same reasoning as every other test in that file.
`ruff check`/`ruff format --check`/`pyright` clean across the whole
repository, not just this file.

## 6. Limitations

* **Still a pull, not a push.** An operator must actually invoke
  `freshness` on some cadence of their own (cron, a healthcheck endpoint
  polled by their monitoring, etc.) -- this phase does not make that
  invocation happen by itself (§4).
* **Only as fresh as the last successful `offhost_backup.py backup` run.**
  If backups are being produced but by some other, unrelated path that
  never calls `record_verified()`, this check cannot see them.
* **Single ledger file.** If `OFFHOST_BACKUP_STATE_DIR` itself is lost
  (Phase 2.42 §13's own limitation), this check correctly reports `FAIL`
  (empty ledger) -- it cannot distinguish "no backups were ever taken"
  from "the ledger recording them was lost," which is the same ambiguity
  Phase 2.42 already documented and did not resolve.
