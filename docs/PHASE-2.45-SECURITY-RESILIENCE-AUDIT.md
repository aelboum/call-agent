# Phase 2.45 — Production Security & Resilience Audit

An independent, read-only re-audit against the mandate's own item 6
("production security and resilience auditing"). `docs/PHASE-2.16-SECURITY
-READINESS.md` was the last dedicated pass, done before the telephony/media
layer (2.21-2.32), call orchestration, backup/recovery (2.37/2.42/2.43), and
operational hardening (health readiness, production metrics) existed. This
phase re-audits everything built since, rather than re-litigating what 2.16
already covered.

## 1. Scope

`docker-compose.yml`/`docker-compose.staging.yml` (container hardening --
2.16's own checklist marked this "Environment-dependent (no container
config exists)," which is now stale: both files exist), the telephony/media
layer's externally-reachable surface, backup/recovery credential handling
end-to-end (cross-checked against what Phase 2.37/2.42/2.43's own docs
claim), `voiceagent/health.py`'s response bodies, and a repo-wide secrets
grep.

## 2. Findings

**0 critical, 0 high, 0 medium, 2 low.** Everything else checked (listed
in §3) was already correctly handled.

1. **Low -- no container-level resource limits.** Neither compose file
   bounded any service's memory/CPU at the container level -- only
   Phase 2.32's own app-level backpressure constrained a runaway process.
   **Fixed**: every service in both files now carries an explicit
   `deploy.resources.limits.memory`/`cpus` (`postgres`/`migrate`/`api`/
   workers/`frontend` at 256m-1g and 0.5-1.0 cpu; `call-runtime` at `1g`/
   `2.0` cpu, the heaviest process, matching Phase 2.32's own sustained-
   concurrency profile). Validated with `docker compose config` (staging
   separately confirmed to parse as valid YAML with `yaml.safe_load`,
   since its `${VAR:?...}` required substitutions cannot resolve without
   a real `.env.staging`).

2. **Low -- implicit WebSocket message-size default.**
   `voiceagent/telephony/freeswitch/media_transport.py`'s
   `serve_freeswitch_media()` called `websockets.serve()` without
   `max_size`, relying on the library's own implicit 1 MiB default rather
   than a reviewed, product-specific bound for this product's own PCM
   audio-frame/JSON-control-message shape. **Fixed**: added
   `_MAX_MEDIA_MESSAGE_BYTES = 64 * 1024` (comfortably above this
   product's largest real format -- 16 kHz mono pcm_s16le, 32,000
   bytes/second -- and any JSON control message, far below the previous
   1 MiB default) and pass it explicitly as `max_size=`.

## 3. Checked and found clean (no new gap; not re-reported)

* Compose files: no hardcoded real secrets (dev defaults clearly marked
  `# pragma: allowlist secret` with a top-of-file note; staging uses
  `${VAR:?...}`/`${VAR:-}` references only), Postgres/Redis never
  published to the host, the media-listener port is intentionally
  published (FreeSWITCH is external) and already gated by Phase
  2.16/2.24's own ticket verification.
* `scripts/db_backup.sh`/`db_restore.sh`/`offhost_backup.py`/
  `db_recovery_drill.py`: credential handling matches what their own
  phase docs claim -- confirmed independently, not merely re-read.
* `voiceagent/health.py`: response bodies carry only `{"status": ...}`,
  never a stack trace, config value, or connection string; the listener
  defaults to `127.0.0.1:9100`, never published as a container port.
* Repo-wide secrets grep: no real hardcoded credential found outside
  already-marked local-dev placeholders.

## 4. Testing performed

`docker compose -f docker-compose.yml config -q` (passes); both files'
`services.*.deploy.resources.limits` validated present via
`yaml.safe_load`. `tests/telephony/freeswitch/test_media_transport.py` --
18/18 pass after the `max_size` change. `ruff check .`/
`ruff format --check .`/`pyright` clean across the whole repository;
`pytest -q -m "not integration"` full suite green.

## 5. Non-goals / limitations

* Did not re-validate every item Phase 2.16 already marked
  Verified/Hardened -- only scope built after that phase.
* Container resource *limits* are new; load-tested *correctness* of those
  specific numbers (will `call-runtime` actually OOM under the
  concurrency levels Phase 2.32 validated, now that it is capped at 1g?)
  was not re-run against live sustained-concurrency traffic in this phase
  -- an operator tuning these for their own real traffic profile should
  treat these as a reviewed starting point, not a load-tested ceiling.
* No destructive or production-infrastructure action was taken; this was
  entirely read-only investigation plus two local, reversible config/code
  edits.
