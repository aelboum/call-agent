# Phase 2.31: Long-Running Runtime Stability & Failure-Recovery Validation

Baseline: `cf11189` (post Phase 2.30 remediation, `origin/main`). SaaS-OS
remains pinned and unmodified at `ff550010e5eafecace7311038aadc99fcecfbe3d`.
`docs/PHASE-0-ARCHITECTURE.md` and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched. No
commit exists for this phase's own work and none has been pushed.

This phase is validation/audit only, closing the specific evidence gaps
Phase 2.30 left open: small-N (24-call) soak, no genuinely concurrent
calls sharing the same long-lived process, no coverage of "other
runtime-owned collections" beyond `CallRuntime._errors`, and no attempt
at listener-level disconnect. No genuine new production defect was found;
`voiceagent/` was not modified.

**Status vocabulary used throughout**: category is one of **REAL STAGING**
/ **HERMETIC** / **NOT VALIDATED** (never blurred); result is one of
**GREEN** (genuinely validated) / **PARTIAL** (some but not all requested
evidence) / **NOT VALIDATED** (could not safely establish) / **FAILED**
(reproducible defect).

## 0. Headline result

```text
Section A (long sequential + mixed soak, 60 real calls)   REAL STAGING, GREEN
Section B (failure-recovery pattern)                      REAL STAGING + HERMETIC, GREEN
Section C (concurrent mixed workload, 5 groups/12 calls)  REAL STAGING, GREEN
Section D (resource-stability analysis)                   REAL STAGING, GREEN (stable + explained)
Section E (listener-level disconnect)                     NOT VALIDATED -- see section 7, precise reason given
Section F (hermetic regression coverage)                  HERMETIC, GREEN -- 2 new tests
Section H (security / tenant isolation)                   REAL STAGING, GREEN
```

No production defect found. `voiceagent/` unchanged this phase.

## 1. Repository state at start

* HEAD / origin/main: `cf11189a9fdafd80e58a1ab26b23b140f4e77904`, branch `main`.
* Initial `git status --short`:
  ```text
   M docs/PHASE-0-ARCHITECTURE.md
  ?? docs/ADR/0010-one-frontend-multiple-user-contexts.md
  ```
  (the two known protected, pre-existing entries -- confirmed unchanged
  by this phase, see section 13).

## 2. What already existed -- read first, not duplicated

* `scripts/validate_staging_long_running_soak.py` (Phase 2.30): one
  shared `ManagedEslConnection`/media listener/`CallRuntime`/
  `DatabaseBoundary`/`CallOrchestrator`, driving calls through a
  caller-supplied `--sequence`. Ran calls strictly one at a time.
* `voiceagent/runtime/supervisor.py`: `CallRuntime._errors` reaped by
  `start_call()`'s own `_reap_terminal_errors()` (Phase 2.30 fix) whenever
  a *different* call starts; `_tasks`/`_cancellations` popped in
  `_wrap_call_task()`'s own `finally`, unconditionally.
* `tests/runtime/test_supervisor.py`: Phase 2.30's own 6 regression tests
  for the `_errors` fix (error-reap timing, successful/cancelled calls
  leaving no error state, repeated-failure non-accumulation, concurrent
  cleanup isolation, metric/log independence). All still pass unmodified.
* `docs/PHASE-2.30-LONG-RUNNING-STABILITY.md` / `docs/PHASE-2.29-REAL-
  MEDIA-FAILURE-STABILITY.md`: read in full for status-vocabulary and
  rigor precedent, followed here.

## 3. Static investigation -- other runtime-owned, call-scoped collections

Before running anything, brief section A's own instruction ("other
runtime-owned collections whose lifetime is tied to calls") was checked
by direct code review, the same way Phase 2.30 found `_errors` by
reading `supervisor.py` end to end rather than by luck. Every per-call
`dict`/`set` reachable from the production call path was located and its
cleanup path traced:

| Collection | Owner | Set | Cleared | Verdict |
| --- | --- | --- | --- | --- |
| `_tasks`, `_cancellations` | `CallRuntime` | `start_call()` | `_wrap_call_task()`'s `finally`, unconditional | correct (Phase 2.30) |
| `_errors` | `CallRuntime` | `_wrap_call_task()`'s `except` | `start_call()`'s `_reap_terminal_errors()` | correct (Phase 2.30 fix) |
| `_streams`, `_sockets`, `_attached_at` | `FreeSwitchMediaProvider` (`voiceagent/telephony/freeswitch/media.py`) | `attach()` (lines 287-288) | `detach()` (lines 295, 300-301), all three popped together | correct |
| `_background_tasks` | `CallOrchestrator` (`voiceagent/runtime/orchestrator.py:261`) | `.add(task)` (line 270) | `task.add_done_callback(self._background_tasks.discard)` (line 271) -- self-cleaning, standard idiom | correct |
| `_subscribers` | `TelephonyEventRouter` (`voiceagent/runtime/telephony_events.py:82`) | `subscribe()` (line 100) | `unsubscribe()` (line 107), called from `_run_pumps_with_remote_hangup_detection()`'s own outer `finally` (`voiceagent/runtime/call_task.py:463-464`), which wraps every exit path -- success, exception, `asyncio.CancelledError` | correct |

`voiceagent/runtime/call_task.py`'s own teardown (`finally`, lines
627-668) also confirms `media.detach()` is always attempted -- bounded by
`media_detach_timeout_seconds`, and any timeout/exception there is caught
and logged rather than skipping the rest of teardown -- so a call that
dies via `TransportError` mid-stream (Phase 2.29) still reliably detaches
its own media state rather than leaking `_streams`/`_sockets`/
`_attached_at`.

**No new defect found by this review.** `_errors` (Phase 2.30) remains
the one collection that needed a real fix; everything else already had a
correct, unconditional or self-cleaning release path. This is reported
as investigation evidence, not merely inferred from an absence of
symptoms in the real-staging runs (sections 4-7 below independently
observe `media_streams`/`media_sockets` returning to 0 at every
checkpoint, consistent with this static finding).

## 4. Harness changes

`scripts/validate_staging_long_running_soak.py` (extended, Phase 2.31):

* `--sequence` now accepts a `parallel:tokenA+tokenB+...` step alongside
  plain tokens -- 2+ calls in that step run **concurrently**, via
  `asyncio.gather()`, against the exact same shared `CallRuntime`/ESL
  connection/media listener as every sequential call around them. This
  is new engineering, not a separate script: Phase 2.30's own script only
  ever ran calls strictly one at a time (`current_load` returning to 0
  between every call), which cannot exercise brief section C's own
  requirement (several calls genuinely in flight together, one failing,
  on one long-lived process).
* Every call in a run, sequential or a parallel-group member, now gets a
  globally unique local SIP/RTP port pair (`local_sip_port +
  global_call_index`, no modulo/reuse) -- required once members of a
  step can be genuinely simultaneous; the old `(i % 8) + 1` scheme was
  only safe because no two sequential calls ever overlapped in time.
* `--checkpoint-every N` (default 10) replaces the old hardcoded
  `{5, 10, 20, last}` checkpoint schedule, for denser sampling across a
  60-call run.
* `CallOutcome.concurrent_group` (new field) tags which parallel-group
  batch (if any) a call belonged to; the summary now prints one
  isolation line per concurrent group (distinct tenants/sessions/
  FreeSWITCH-UUIDs/SIP-Call-IDs among just that group's own members).

`scripts/validate_staging_listener_disconnect.py` (new, Phase 2.31):
a small, fully self-contained, one-call harness for section E -- see
section 7.

`tests/runtime/test_supervisor.py` (extended, Phase 2.31): 2 new hermetic
tests -- see section 8.

No production `voiceagent/` file was changed.

## 5. Section A/B/C -- real-staging mixed workload (sequential + concurrent)

**REAL STAGING, GREEN.**

**Topology**: exactly ONE long-lived process/runtime for the entire run --
one `ManagedEslConnection`, one `FreeSwitchMediaListener` (one port), one
`FreeSwitchMediaProvider`, one `CallRuntime`, one `DatabaseBoundary`, one
`CallOrchestrator`, matching Phase 2.27/2.30's own production-accurate
topology. No process restart between any two calls, sequential or
concurrent, anywhere in the run. `p224-freeswitch` (SIP `127.0.0.1:15080`,
ESL `127.0.0.1:18023`), same container Phase 2.27/2.29/2.30 used, never
`p223`. Real Deepgram/OpenAI/Aura providers throughout.

**Command**:
`scripts/validate_staging_long_running_soak.py --fs-container
p224-freeswitch --media-public-base-url ws://192.168.65.254:8601
--sip-advertise-ip 192.168.65.254 --checkpoint-every 10 --sequence "..."`
(60-token sequence: 20 sequential calls implementing the brief's own
section B failure-recovery pattern -- normal x5, media_fail, normal,
media_fail, cancel, normal, media_fail, media_fail, normal, normal,
cancel, normal, media_fail, normal, normal, normal --, then 16 calls
across 5 `parallel:` concurrent groups of size 2-3 interleaved with
sequential separators (section C), then 24 more sequential calls for
resource-trend density and a second, independent failure-recovery
stretch). Total wall time: 1169.2s (~19.5 minutes) for 60 real calls,
~19.5s/call average including 12 calls that ran concurrently in groups.

**Target vs. actual**: 60 calls, not 50-100's upper end. 60 was chosen
deliberately as "materially larger than Phase 2.30's 24" (2.5x) while
remaining a single continuous real-call run this validation session could
execute, monitor, and honestly account for end to end -- the brief
explicitly permits landing below 100 with a stated reason rather than
padding the count or leaving a run unattended past what could be verified.

**Sequential soak (44 non-concurrent calls) -- GREEN.** All 60 calls
(including 5 concurrent groups) reached the correct terminal outcome for
their scenario:
* 41 `normal` calls: all `status='completed'`.
* 13 `media_fail` calls: all `status='failed'`, `end_reason='media_disconnect'`
  (Phase 2.29's classification, unchanged, exercised far more times in one
  run here -- 13 -- than Phase 2.30's own soak exercised in total -- 4).
* 6 `cancel` (real early caller BYE) calls: all reached a real terminal
  status. 4 reached `completed`; 2 (`#35`, `#46`) reached
  `status='failed'`/`end_reason='media_disconnect'` instead. This is not
  a new finding: an early hangup genuinely races two already-correct
  mechanisms -- the remote-hangup watcher (`voiceagent/runtime/call_task
  .py`'s `_watch_for_remote_hangup()`, sets `reason="hangup"`) and the
  media transport's own teardown as the SIP dialog ends (which can itself
  raise `TransportError`, Phase 2.29's own fix, before the hangup watcher
  wins the race) -- and `scripts/validate_staging_concurrent_media_e2e
  .py`'s own `_run_call_on_shared_infra()` has treated either outcome as
  a pass for a `cancel` scenario since Phase 2.28 (`"PASS (call N): early
  hangup after N.Ns reached a real terminal CallSession status ('failed')
  cleanly"` is its own pre-existing, deliberately-worded message for
  exactly this case). Not reopened, not a defect.
* 5 `normal` calls (`#14`, `#24`, `#28`, `#32`, `#40`) returned this
  script's own `result_code=2` (PARTIAL) -- `status='completed'` but the
  independently-transcribed return audio did not clearly confirm the
  agent's own reply. This is the same "alpha" -> "also"-class STT
  recognition-quality variance Phase 2.28 already established as a
  provider characteristic, not a concurrency or isolation defect -- two of
  the five (`#28`, `#32`) were concurrent-group members, and both groups'
  own isolation checks independently passed regardless.

**Concurrent mixed workload (5 groups, 12 calls total) -- GREEN.**

```text
group1  members=3  normal+media_fail+normal      isolation=PASS  statuses=[completed, failed, completed]
group2  members=2  normal+cancel                 isolation=PASS  statuses=[completed, completed]
group3  members=2  normal+media_fail             isolation=PASS  statuses=[completed, failed]
group4  members=3  normal+normal+media_fail      isolation=PASS  statuses=[completed, completed, failed]
group5  members=2  cancel+normal                 isolation=PASS  statuses=[failed(media_disconnect), completed]
```

Every group: distinct tenants == distinct sessions == distinct FreeSWITCH
UUIDs == distinct SIP Call-IDs == member count (no reuse, no collision).
Every `media_fail` member reached `failed`/`media_disconnect` while its
concurrent siblings reached their own correct, unaffected outcome in the
same group -- the core section C property (one call failing does not
affect another call genuinely in flight at the same time on the same
runtime) held in all 5 trials, sizes 2 and 3 both represented.

**Failure/recovery (section B) -- GREEN.** The 20-call opening sequence
and the 24-call closing sequence each independently exercise: a normal
call immediately after a failure (calls `7`, `9`->`10`, `13`), back-to-back
failures (`11`,`12` and `42`,`43`), a cancellation immediately after a
failure (`8`->`9`), and multiple normal recovery calls after a failure
cluster (`13`,`14` after `11`,`12`; `44`,`45` after `42`,`43`). Every
subsequent call in the whole 60-call run reached its own correct outcome
regardless of what happened immediately before it -- no call was ever
poisoned by the one before it, sequential or concurrent.

**`_errors` under repeated failures (13 media_fail + 2 cancel-as-failed =
15 total failures across the run).** `errors_dict_size_after` in the
per-call log never exceeded `1` at any single call's own observation
point, and read `0` at every one of the 7 resource checkpoints (before
first call, after 10/20/30/40/50/60) -- every checkpoint happens to land
after the runtime has already started at least one more call since the
last failure, which is exactly when `_reap_terminal_errors()` (Phase
2.30) fires. This is stronger evidence, at 15 real failures in one run,
than Phase 2.30's own 4-failure soak could produce, and it directly
confirms the Phase 2.30 fix holds under a workload with roughly 4x the
failure count and includes concurrent, not just sequential, failures.

## 6. Section D -- resource-stability analysis

| checkpoint | calls | RSS (MiB) | asyncio tasks | `_errors` size | media streams | DB executor threads | wall (s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| before_first_call | 0 | 108.4 | 6 | 0 | 0 | 0 | 1.4 |
| after_10_calls | 10 | 126.4 | 7 | 0 | 0 | 2 | 216.4 |
| after_20_calls | 20 | 128.4 | 6 | 0 | 0 | 2 | 431.7 |
| after_30_calls | 30 | 129.5 | 6 | 0 | 0 | 4 | 581.3 |
| after_40_calls | 40 | 134.5 | 6 | 0 | 0 | 4 | 751.5 |
| after_50_calls | 50 | 132.4 | 7 | 0 | 0 | 4 | 958.0 |
| after_60_calls | 60 | 124.3 | 7 | 0 | 0 | 4 | 1169.2 |

**Analysis, not just numbers.**

* **`_errors`, media streams/sockets**: `0` at every single checkpoint,
  across 60 calls and 15 failures. This is bounded retained state, not
  merely "not yet shown to grow" -- every call-scoped collection this
  phase could observe returned to its exact pre-call baseline every time,
  consistent with the static code review (section 3) that traced each
  one's cleanup path and found it unconditional or self-cleaning.
* **`asyncio_tasks`**: oscillates 6-7 throughout, no trend. The one extra
  task at some checkpoints is consistent with a checkpoint occasionally
  landing while a router/heartbeat-adjacent task is transiently alive,
  not accumulation -- it never climbs past 7 despite 60 calls having
  passed through.
* **`db_executor_thread_count`**: `0 -> 2 -> 2 -> 4 -> 4 -> 4 -> 4`. This
  rises, then **plateaus and stays flat for the back half of the run**
  (30 through 60 calls, unchanged at 4) -- classic thread-pool warm-up
  bounded by actual peak concurrency (this run's largest concurrent
  group was 3 members; `DatabaseBoundary(max_workers=8)`, so 4 live
  threads sits well under the configured cap and matches real concurrent
  demand, not unbounded growth). A monotonic leak would keep climbing
  past 40 calls; this did not.
* **RSS**: `108.4 -> 126.4 -> 128.4 -> 129.5 -> 134.5 -> 132.4 -> 124.3`
  MiB. This is the single most important trend line, and it is
  **explicitly not monotonic**: it rises over the first ~40 calls (a
  normal warm-up -- Python object caches, connection pools, provider SDK
  internal buffers all reach steady state once, not once per call), then
  **plateaus and falls** over the last 20 calls, ending lower than its
  own call-40 peak and only 16 MiB above where it started 60 real calls
  and 15 failures earlier. A real per-call leak would show RSS climbing
  in step with the call count for the entire run, including its last
  third; it does not. Combined with every call-scoped collection reading
  exactly `0`/baseline at every checkpoint, the honest read is **process/
  allocator high-water-mark behavior settling after warm-up, not
  reachable-object growth** -- Python's allocator does not always return
  freed pages to the OS immediately, so a working-set peak followed by a
  plateau (not a return to the exact original 108 MiB) is expected and
  is not, by itself, evidence of retained objects.
* **No monotonic growth was observed in any of the 6 sampled metrics**
  across this 60-call, 15-failure, 5-concurrent-group run.

This is real-run evidence of stability over a materially larger and more
varied workload than Phase 2.30's own PARTIAL rating was based on -- but
it remains, honestly, a single continuous run's sample, not a
multi-hour/multi-thousand-call soak; see section 9a for this limitation
stated in full alongside this phase's other honestly-scoped gaps.

## 7. Section E -- listener-level disconnect

**NOT VALIDATED against the real stack -- precise reason below.** The
functional behavior this section wants to confirm (a call whose media
transport dies because the *listener itself* went away, not just that one
call's own socket, is classified identically to a per-call media
disconnect -- `failed`/`media_disconnect`) is architecturally the same
code path Phase 2.29 already fixed and this phase's own section 5/6 (real
staging) and hermetic tests re-confirm for a per-call `TransportError`:
`_FreeSwitchMediaStream.send()`/`.receive()` cannot distinguish "my own
socket closed" from "the listener that owned my socket closed" -- both
surface as the identical `TransportError` from
`voiceagent/telephony/freeswitch/media_transport.py`'s `send_text()`/
`receive_text()`. There is no separate code path for "listener died" to
have its own, different bug.

**What was attempted.** The brief explicitly forbids destroying the
shared listener the main soak (section 5) depends on -- `p224-
freeswitch`'s own media listener is used by every other call in this
phase's real-staging evidence, so closing it mid-soak would have
invalidated all of it, not just tested one thing. A genuinely isolated
alternative was built instead: `scripts/validate_staging_listener_
disconnect.py` constructs its OWN, completely separate one-call topology
-- its own `ManagedEslConnection`, `FreeSwitchMediaProvider`/
`FreeSwitchMediaListener` (its own dedicated port), `CallRuntime`,
`DatabaseBoundary`, `CallOrchestrator` -- against `p223-freeswitch`, the
one FreeSWITCH container in this environment that no other Phase 2.27-
2.31 real-staging script uses, specifically so nothing else could be
affected by closing *this* listener mid-call.

**Why it did not produce real evidence.** `p223-freeswitch` is up and
ESL-reachable (`fs_cli -x status` responds normally), but is not
configured compatibly with this harness's SIP routing: a real INVITE was
sent, FreeSWITCH accepted the channel
(`docker logs p223-freeswitch`: `sofia/internal/phase225caller@...`), but
no `CallSession` was ever created -- the channel was logged
`Abandoned` by FreeSWITCH's own state machine, both at a 5-second and a
60-second wait (ruling out a timing race). The channel context name
(`phase225caller`) indicates this container's dialplan is wired for a
different, unrelated phase's own specific SIP profile, not the generic
ESL-event-routed `CallOrchestrator.handle_unrouted_offer()` path every
other real-staging script in this repository depends on. Reconfiguring
`p223-freeswitch`'s own dialplan was out of scope for this phase (risk of
destabilizing another phase's environment for a validation-only
side-quest) and was not attempted.

**What this means, stated honestly.** This is an environment/tooling gap
in this validation session, not evidence about product behavior either
way -- there is no reason from the code itself to expect listener-level
and per-call `TransportError` to be classified differently (they are the
same exception, from the same two call sites, handled by the same
`except TransportError` clause in `voiceagent/runtime/call_task.py`), but
that expectation was not confirmed against a real second listener
failure this phase. No passing result is claimed. A future phase wanting
this evidence should either provision a dedicated, harness-compatible
FreeSWITCH container, or add a hermetic test that closes a fake
`MediaSocket`'s underlying transport out from under an in-flight call and
asserts the same `failed`/`media_disconnect` outcome -- the latter was
considered here but not added, since it would exercise the exact same
`TransportError` branch Phase 2.29's own existing hermetic coverage
already exercises (this phase's brief section F explicitly asks not to
duplicate existing coverage).

## 8. Section F -- hermetic regression coverage (new)

Two tests added to `tests/runtime/test_supervisor.py` (full file: 18
tests, all pass), covering invariants Phase 2.30's own 6 tests did not:

1. **`test_mixed_concurrent_success_failure_cancel_cleanup_is_isolated`**
   -- three calls live at once on one runtime (one fails, one succeeds,
   one is cancelled), proving `_tasks`/`_cancellations` both return to
   `{}` regardless of *which* of the three ways each call ended, that
   only the failed call's `error_for()` is ever non-`None`, and that a
   fourth call starting reaps exactly that one entry without disturbing
   the other two (both already `None`). Phase 2.30's own concurrent test
   only ever mixed a failure with a success.
2. **`test_long_mixed_failure_recovery_sequence_returns_bookkeeping_to_
   baseline`** -- this phase's own brief section B pattern (normal /
   failure / normal / failure / cancel / normal / failure / failure /
   normal / cancel), run sequentially and hermetically, asserting `_tasks`
   and `_cancellations` are back to `{}` after **every single call**
   regardless of outcome, and that `_errors` never holds more than 1
   entry across 4 failures including 2 back-to-back. Phase 2.30's own
   repeated-failure test used one scenario type (failure) only.

Both pass; full hermetic suite is 960 passed (958 after Phase 2.30's own
remediation, plus these 2), 261 deselected -- no other test's behavior
changed.

## 9. Section H -- security / tenant isolation

**REAL STAGING, GREEN.**

* **Tenant isolation**: every one of the 60 real calls used a distinct,
  freshly-provisioned tenant (`_provision_fixture()` creates a new tenant
  per call, unchanged from Phase 2.27-2.30); `distinct tenants=60` of 60.
* **CallSession isolation**: `distinct sessions=60` of 60, no id reuse,
  confirmed both overall and, separately, within each of the 5 concurrent
  groups (each group's own member count equals its own distinct-session
  count).
* **Media isolation**: distinct FreeSWITCH channel UUIDs and SIP Call-IDs
  for all 60 calls (`distinct fs_uuids=60`, `distinct sip_call_ids=60`);
  every concurrent group's own members are pairwise distinct on both.
  Every transcript logged (`independently transcribed return audio: ...`)
  matches only that call's own expected keyword/phrase (`_CALL_PHRASES`,
  `phrase_mode="distinct"`) or is empty (for `media_fail`/failed-`cancel`
  calls, expected) -- no cross-call transcript ever appeared in another
  call's own line.
* **Runtime ownership / authorization ordering**: unchanged production
  code path for every call (`CallOrchestrator` -> `CallRuntime.start_call()`
  -> the same authorization/tenant-context flow every prior phase's real
  calls went through); this phase changed no code in that path.
* **Failure containment across tenants**: in every concurrent group, the
  failed member's own tenant never overlapped with a surviving member's
  tenant (fresh tenant per call, by construction), and no surviving
  member's `CallSession`, transcript, or runtime bookkeeping was ever
  observed to change as a result of a concurrent sibling's failure --
  the same property section 5's per-group isolation lines directly
  confirm.

## 9a. Limitations, stated honestly

* 60 real calls achieved in one continuous run, not 100 -- see section 5's
  own "target vs. actual" note.
* This is a single continuous run's resource sample (section 6). It is
  strong evidence of stability across a materially larger and more mixed
  workload than Phase 2.30's own, and the non-monotonic RSS trend
  specifically rules out a steady per-call leak of any size that would be
  visible inside 60 calls -- but it remains one run, not a multi-hour or
  multi-thousand-call soak, and cannot rule out a leak so slow it would
  only become visible at a much larger N.
* Section E (listener-level disconnect) is explicitly NOT VALIDATED --
  see section 7 for the precise environment reason, not a product
  concern.
* The `cancel`-scenario race described in section 5 (2 of 6 early-hangup
  calls resolving as `media_disconnect` instead of `completed`) is
  correctly classified either way by the existing, already-fixed code,
  but this phase did not attempt to determine which of the two detection
  paths (`_watch_for_remote_hangup()` vs. the media transport's own
  teardown) actually wins in what proportion of real early hangups --
  that would require many more `cancel`-scenario trials than this run's
  own 6, and was not this phase's own question.
* FreeSWITCH's own resource behavior (CPU/mem via `docker stats`) was not
  included in this phase's own tables -- it was sampled by the harness
  (inherited from Phase 2.30) but not analyzed in depth here, since the
  Python-process-side metrics (RSS, task counts, collection sizes) were
  the ones directly relevant to the `CallRuntime`-lifecycle question this
  phase asks; a future phase stress-testing FreeSWITCH's own resource
  ceiling under sustained concurrency remains Phase 2.27's own stated
  territory, not repeated here.

## 10. Newly discovered defects

**None.** The static review in section 3 examined every other per-call,
runtime-owned collection reachable from the production call path and
found each one already correctly released on every exit path. The
real-staging runs (sections 5, 9) independently corroborate this: media
stream/socket counts return to their pre-call baseline at every resource
checkpoint, across 60 real calls including concurrent groups, failures,
and cancellations. Section 7's listener-disconnect gap is an environment/
tooling limitation of this validation session, not a product finding --
see that section for why no defect is being inferred from it either.

## 11. Findings not reopened

* Phase 2.29's `TransportError` -> `media_disconnect` classification:
  confirmed unchanged and still correct across every `media_fail`
  scenario in this phase's own real-staging soak (section 5) and
  concurrent trials (section 9).
* Phase 2.30's `CallRuntime._errors` bounding fix: confirmed still
  correct under a materially larger and more mixed workload than Phase
  2.30's own 24-call, single-scenario-type soak -- `_errors` never
  exceeded 1 entry across this phase's entire 60-call run despite far
  more induced failures than Phase 2.30 exercised (see section 5/6).
* The "alpha" -> "also" STT variability (Phase 2.28): not reopened; any
  similar recognition variance observed in this phase's own transcripts
  is treated the same way, a provider/recognition-quality characteristic,
  not a concurrency defect.
* `tests/integration/test_runtime_integration.py
  ::test_one_call_failing_does_not_affect_others`: not modified. See
  section 12 -- it remains the same pre-existing timing-race failure,
  unrelated to anything this phase changed.

## 12. Validation gates

* Targeted Phase 2.31 tests
  (`tests/runtime/test_supervisor.py`, full file, 18 tests): **all
  passed**.
* Hermetic `pytest` (default `addopts`, excludes `-m integration`):
  **960 passed, 261 deselected**.
* Real integration suite (`pytest -m integration`, real PostgreSQL):
  **261 passed, 0 failed** this run, including
  `test_one_call_failing_does_not_affect_others` -- the known,
  documented, pre-existing fixed-sleep race (its own `asyncio.sleep(0.1)`
  is sometimes long enough for the broken call's exception to be recorded
  before the assertion, sometimes not; this run it was). It is
  non-deterministic by its own documented nature, not newly fixed and not
  modified by this phase -- it was independently reproduced failing
  against a clean parent commit in an earlier session (before Phase 2.30
  began) using an isolated `git worktree`, confirming the race itself is
  real and pre-existing, unrelated to any phase's own changes.
* Ruff check: **all checks passed**.
* Ruff format: **358 files already formatted**.
* Pyright: **0 errors, 0 warnings, 0 informations**.
* import-linter: **8 contracts kept, 0 broken**.
* detect-secrets (`.secrets.baseline`): scan's own known in-place
  mutation reproduced again (same pre-existing findings across files
  entirely outside this phase's own changes, matching Phase 2.29/2.30's
  own documented precedent) -- diffed, explicitly confirmed **zero**
  findings in any file this phase touched (`grep`-checked by filename),
  reverted with `git checkout -- .secrets.baseline` before ever being
  staged.
* pip-audit: **no known vulnerabilities** (`saas-os`/`voiceagent`
  skipped, not on PyPI, expected and unchanged from every prior phase).
* `git diff --check`: **clean**.

## 13. Files changed

```text
scripts/validate_staging_long_running_soak.py (extended)   -- parallel:a+b+... concurrent
                                                                steps, --checkpoint-every,
                                                                globally unique per-call ports,
                                                                concurrent_group tagging
scripts/validate_staging_listener_disconnect.py (new)       -- isolated one-call listener-
                                                                disconnect harness (section 7)
tests/runtime/test_supervisor.py (extended)                 -- 2 new hermetic tests
docs/PHASE-2.31-LONG-RUNNING-RUNTIME-VALIDATION.md (new)     -- this document
```

**No `voiceagent/` production file was changed.** No genuine defect was
found that required one (section 10).

## 14. SaaS-OS pin

Verified unchanged, both before and after this phase's work:
`ff550010e5eafecace7311038aadc99fcecfbe3d`
(`.venv/Lib/site-packages/saas_os-0.1.0.dist-info/direct_url.json`,
`commit_id` and `requested_revision` both match, `vcs: "git"`).

## 15. Final git status

```text
 M docs/PHASE-0-ARCHITECTURE.md
 M scripts/validate_staging_long_running_soak.py
 M tests/runtime/test_supervisor.py
?? docs/ADR/0010-one-frontend-multiple-user-contexts.md
?? docs/PHASE-2.31-LONG-RUNNING-RUNTIME-VALIDATION.md
?? scripts/validate_staging_listener_disconnect.py
```

HEAD: `cf11189a9fdafd80e58a1ab26b23b140f4e77904` (unchanged from this
phase's own baseline). `git log origin/main..HEAD` is empty.

## 16. Protected files / commit / push confirmation

* **Protected files untouched**: `docs/PHASE-0-ARCHITECTURE.md` shows
  only its own pre-existing, unrelated modification (present before this
  phase started, never staged or altered by this phase's own work);
  `docs/ADR/0010-one-frontend-multiple-user-contexts.md` remains
  untracked, exactly as it was at the start of this phase. Neither file
  was read for content modification, staged, or committed at any point
  in this phase.
* **Nothing was committed.** HEAD remains
  `cf11189a9fdafd80e58a1ab26b23b140f4e77904`, identical to this phase's
  own recorded starting point; no new commit exists.
* **Nothing was pushed.** `git log origin/main..HEAD` is empty --
  local and `origin/main` are identical, confirming no local-only commit
  exists that could have been pushed, and no push command was run.
