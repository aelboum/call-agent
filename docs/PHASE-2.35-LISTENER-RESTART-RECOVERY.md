# Phase 2.35 — Real Listener Restart / Recovery Validation

Status: **implemented, not yet executed.** This doc closes a documentation
gap found during Phase 2.43-2.45 reconciliation: `scripts/validate_staging
_listener_restart_recovery.py` existed (uncommitted, written against Phase
2.33's `media_transport.py` fix) with no status report of its own, unlike
every other `validate_staging_*.py` script in this repository.

## 1. What this validates

Builds on Phase 2.33's own `media_transport.py` fix (`listener_closing`
threaded through `serve_freeswitch_media()`/`handle_connection()`, proven
hermetically by `tests/telephony/freeswitch/test_media_transport.py`'s own
Phase 2.33 cases). This script validates the real-staging operational
consequence, against a real shared media listener:

```
listener running -> real call in_progress -> listener-wide Server.close()
-> active call fails failed/media_disconnect -> listener restarted and
verified ready (real TCP-connect probe, never a bare sleep()) -> completely
fresh real call -> completed/completed
```

Three tests sequentially, reusing `_provision_fixture`/
`_run_call_on_shared_infra`/`CallFixture` from
`validate_staging_concurrent_media_e2e.py` and `_sample`/
`ResourceCheckpoint` from `validate_staging_long_running_soak.py`
unchanged -- the same reuse discipline every other `validate_staging_*.py`
script in this repository already follows.

## 2. Work done this reconciliation pass

The script had 5 lint/type defects when found (unused import, one `S104`
false-positive missing its `noqa`, two `E501` line-length violations, one
`reportPossiblyUnboundVariable` on `evidence2` when call 2 never reaches
`in_progress` or times out after the listener closes). All fixed; `ruff
check`/`ruff format --check`/`pyright` now clean. No behavioral change --
the `evidence2` fix only makes an already-intended fallback (`None` when
call 2's own evidence was never captured) type-correct and crash-proof;
the test scenario's actual pass/fail logic is unchanged.

## 3. Why this has not been executed

Real-staging test tooling (its own docstring: "Staging-only test tooling,
not product code, not imported by anything under `voiceagent/`"). Running
it for real requires exactly the external prerequisites
`docs/PHASE-2.21-FREESWITCH-TELEPHONY-INTEGRATION.md` and
`docs/PHASE-2.27-CONCURRENT-MEDIA-VALIDATION.md` already named for their
own real-staging runs, none of which exist in this sandbox:

* A reachable staging FreeSWITCH instance with ESL enabled
  (`VOICEAGENT_FREESWITCH_ESL_HOST`/`_PORT`, `FREESWITCH_ESL_PASSWORD`).
* A real SIP trunk/softphone path this script's own `sip_host`/`sip_port`/
  `sip_advertise_ip` arguments can place calls through (`scripts/sip_uac.py`).
* A real AI provider key (`VOICEAGENT_AI_ELIGIBLE_PROVIDERS` plus the
  matching `OPENAI_API_KEY`/etc.) for the engine session each test call
  actually runs.
* A bootstrapped tenant (`scripts/bootstrap_rbac.py`).

None of these exist in this development sandbox, and provisioning them is
outside this session's authorization (no new paid service, no production
credential, no infrastructure change without explicit approval). This is
the same class of blocker already disclosed for off-host remote-storage
validation (Phase 2.42 §13) and the recovery drill's own integration test
(Phase 2.43 §11) -- all three ultimately need a real external environment
this sandbox cannot provision for itself.

## 4. Testing performed

Hermetic only: `ruff check`, `ruff format --check`, `pyright` -- all clean.
`tests/telephony/freeswitch/test_media_transport.py` (the hermetic Phase
2.33 coverage this script's own real-staging scenario builds on) -- 18/18
pass. The script itself was not run; see §3.

## 5. Exact next step once infrastructure exists

```
python scripts/validate_staging_listener_restart_recovery.py \
    --sip-host <staging-freeswitch-host> --sip-port 5060 \
    --sip-advertise-ip <this-host-public-ip> \
    --media-listen-host 0.0.0.0 --media-listen-port 8100
```

Exit code `0` and every `result["tests"]`/`result["identifier_isolation"]`
entry `PASS` is the acceptance bar -- identical reporting shape to
`validate_staging_concurrent_media_e2e.py`'s own convention.
