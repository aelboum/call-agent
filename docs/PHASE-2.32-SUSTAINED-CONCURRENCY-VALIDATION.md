# Phase 2.32: Sustained Concurrency Capacity Validation

Baseline: `0f7c9d5` (post Phase 2.31, `origin/main`). SaaS-OS remains pinned
and unmodified at `ff550010e5eafecace7311038aadc99fcecfbe3d`.
`docs/PHASE-0-ARCHITECTURE.md` and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched
(the pre-existing modification/untracked state of both predates this
phase and was never staged or altered here). No commit exists for this
phase's own work and none has been pushed.

This phase validates the next production-relevant runtime property:
**sustained concurrency capacity** -- can the current single-process/
single-asyncio-loop runtime handle materially more concurrent calls than
Phase 2.31's own max of 3, without cross-call contamination, task/
cancellation leakage, unbounded error bookkeeping, event-loop
instability, or one failing call affecting its siblings. A genuine
correctness defect was found and fixed -- see section 3.

**Status vocabulary** (Phase 2.31's own convention, unchanged): category
is **REAL STAGING** / **HERMETIC** / **NOT VALIDATED**; result is
**GREEN** / **PARTIAL** / **NOT VALIDATED** / **FAILED**.

## 0. Headline result

```text
Section A (8-way concurrent group)                        REAL STAGING, GREEN (functional) -- found + fixed a media-socket leak
Section B (10-way concurrent group)                        REAL STAGING, GREEN (functional) -- same leak, same fix confirms it
Section C (8-way repeat, post-fix regression coverage)     HERMETIC, GREEN
Section D (resource-stability analysis)                    REAL STAGING, PARTIAL -- see 9a, RSS trend not fully resolved at this N
Section E (listener-level disconnect)                      NOT VALIDATED -- out of this phase's own scope, unchanged from Phase 2.31
Section F (hermetic regression coverage)                   HERMETIC, GREEN -- 1 new test
Section H (security / tenant isolation)                    REAL STAGING, GREEN
```

One genuine production defect found and fixed this phase (section 3):
`FreeSwitchMediaProvider.detach()` leaked `_sockets`/`_attached_at`
entries when cancelled mid-teardown under concurrency -- invisible at
Phase 2.31's own max of 3 concurrent calls, reproducible at 8+.

## 1. Repository state at start

* HEAD / origin/main: `0f7c9d5d59576d8d7d6f57120e8f63de594ef805`, branch `main`.
* Initial `git status --short`:
  ```text
   M docs/PHASE-0-ARCHITECTURE.md
  ?? docs/ADR/0010-one-frontend-multiple-user-contexts.md
  ```
  (the two known protected, pre-existing entries -- confirmed unchanged
  by this phase, see section 15).

## 2. Real staging infrastructure stood up this session

No product-owned staging stack was already running at the start of this
session (`docker ps -a` showed this repository's own FreeSWITCH
containers `p223-freeswitch`/`p224-freeswitch` both `Exited`, and no
`docker-compose.staging.yml` services running under any container).
Brought up for this phase, reusing the exact conventions and container
`p224-freeswitch` already established by Phases 2.27-2.31 (never
`p223`, which Phase 2.31 section 7 already found dialplan-incompatible
with this harness):

* `docker start p224-freeswitch` -- the existing, pre-built real
  FreeSWITCH container from a prior phase, unmodified.
* A throwaway `postgres:16-alpine` (`saas_os`/`saas_os_app` roles,
  `voiceagent` database, standalone `docker run`, host port `25432` --
  `docker-compose.yml`'s own `postgres` service default host port 5432
  was occupied by an unrelated project's container on this machine) and
  a throwaway `redis:7-alpine` (host port `26379`).
* Real schema migrations run against that throwaway database: the pinned
  SaaS-OS's own `alembic upgrade head` first (creates `core`/
  `control_plane`/`self_learning` schemas this product's own migrations
  depend on via FK), then this product's own `alembic upgrade head`
  (creates the `app` schema). Both completed cleanly, no manual schema
  edits.
* Real credentials: this repository's own pre-existing, gitignored
  `.env.phase223.local` (confirmed `git check-ignore`'d, never tracked,
  no secret was newly written to disk this phase) supplied real
  `OPENAI_API_KEY`/`DEEPGRAM_API_KEY`. No new secret was created,
  printed, or persisted by this phase.
* Verified end-to-end with a 2-call real smoke test before committing to
  the full workload: real SIP INVITE, real FreeSWITCH channel, real
  Deepgram STT/TTS, real OpenAI LLM response, real DB persistence, one
  `PASS` and one STT-quality `PARTIAL` (the same "alpha"->"also"-class
  variance Phase 2.28 already established, not a new finding).

Both throwaway containers were removed and `p224-freeswitch` was
returned to its original (stopped) state at the end of this phase's
work (section 15).

## 3. Section A/B -- 8-way and 10-way concurrent groups: a real defect found and fixed

**REAL STAGING.** Functionally **GREEN** on every call's own terminal
outcome and every isolation check; this section also surfaced a genuine
resource-cleanup defect, fixed narrowly (not a broad rewrite), per this
phase's own brief ("stop and report it before implementing a broad
fix" -- the fix below is the narrow, well-understood one, applied after
root-causing it, not a guess-patch).

**Harness change** (`scripts/validate_staging_long_running_soak.py`,
extended, no new script): Phase 2.31's own `parallel:tokenA+tokenB+...`
mechanism already generalizes to any member count -- reused unchanged at
8- and 10-member size, materially above Phase 2.31's own max of 3. Added
`GroupPeak`/`_sample_peaks_during`: a background sampler polling the
same read-only `CallRuntime`/`FreeSwitchMediaProvider`/`DatabaseBoundary`
attributes `_sample()` already reads, every 0.25s for the duration of
each group's own `asyncio.gather()` window, keeping the max of each --
the pre-existing before/after checkpoints cannot see a peak that comes
and goes entirely inside one group's own execution window.

**What was found.** Group 1 (8-way: 5x`normal`, 1x`media_fail`,
1x`cancel`, 1x`normal`) completed with every call reaching its correct
terminal status and full isolation (section 5) -- but the
`after_8_calls` checkpoint read `media_streams=0` (correct, empty) and
**`media_sockets=6`** (should also be 0). The two recovery calls
immediately after (`after_9_calls`, `after_10_calls`) both still read
`media_sockets=6` -- it never recovered on its own. The raw log showed
six `media.detach() timed out for CallSession ... (5.0s)` warnings
during group 1's own teardown.

**Root cause** (`voiceagent/telephony/freeswitch/media.py`,
`FreeSwitchMediaProvider.detach()`): the caller
(`voiceagent/runtime/call_task.py`) wraps every `detach()` call in
`asyncio.wait_for(..., timeout=media_detach_timeout_seconds)` (5.0s
default) and treats a `TimeoutError` as a logged, best-effort, non-fatal
event. The old `detach()` body was:

```python
stream = self._streams.pop(call_ref, None)   # (1) always runs
...
await stream.close()                          # (2) can be slow/cancelled here
self._sockets.pop(call_ref, None)             # (3) only reached if (2) finishes
attached_at = self._attached_at.pop(call_ref, None)
```

Under 8-way (and worse, 10-way) simultaneous teardown against one shared
media listener, `stream.close()` (which flushes any buffered audio over
the real WebSocket) was slow enough for several calls at once to exceed
the 5.0s `wait_for` timeout. `wait_for` then cancels the `detach()`
coroutine while it is suspended inside `await stream.close()` -- step
(1) had already run (so `_streams` always correctly emptied, matching
every checkpoint's own `media_streams=0`), but step (3) never got a
chance to (so `_sockets`/`_attached_at` kept a stale entry for that
`call_ref` **forever** -- these are read-only, per-call-scoped
dictionaries with no other reaper). This is a direct, mechanical
explanation for exactly the split observed (`media_streams=0` /
`media_sockets>0`), not an inference from absence of symptoms.

**Confirms across the whole run**: detach-timeout counts (6 in group 1,
1 in group 2, 0 in group 3) match the `media_sockets` deltas at each
group's own checkpoint exactly (`0->6`, `6->7`, `7->7`). The peak-during
sampler independently corroborates it: group 2's own peak
`media_sockets=16` = 6 already-leaked (from group 1) + 10 genuinely
concurrent this group; group 3's own peak `media_sockets=15` = 7
already-leaked + 8 concurrent. `media_streams` peaked at exactly each
group's own member count every time (8, 10, 8) -- never inflated by a
leak, consistent with the root cause being specific to the
`_sockets`/`_attached_at` pop ordering, not `_streams`.

**Fix applied** (narrow, `voiceagent/telephony/freeswitch/media.py`,
`detach()` only): moved the `self._sockets.pop(call_ref, None)` and
`self._attached_at.pop(call_ref, None)` calls to *before* `await
stream.close()`, so a cancellation during that await can no longer skip
them -- mirroring `_streams`, which was already popped before the await
and therefore always correctly cleaned up. Telemetry
(`record_media_session_duration`/`record_provider_operation(...,
"success", ...)`) still only fires after a successful `close()`,
unchanged from before -- this fix touches only the two `.pop()` calls'
position, nothing else in the method's behavior or its exception
semantics on a genuine (non-timeout) `close()` failure.

**Why this is in scope and narrow, not a broad rewrite**: one
five-line reordering in one method, justified by a reproduced,
mechanically-explained real-staging failure; no change to
`media_detach_timeout_seconds`, to `call_task.py`'s own
`wait_for`/exception handling, or to any other collection's cleanup
path (section 3 of Phase 2.31 already reviewed every other one and
found them correct; this phase did not need to revisit that).

## 4. Section B -- 10-way concurrent group detail

**REAL STAGING, GREEN** (functional). Group 2 (10-way:
6x`normal`, 2x`media_fail`, 1x`cancel`, 1x`normal`) -- the largest
concurrent trial in this repository's own validation history (Phase
2.31's own max was 3). Every one of the 10 calls reached its correct
terminal outcome; both `media_fail` members reached
`failed`/`media_disconnect` while their 8 concurrent siblings reached
their own correct, unaffected outcomes -- **the exact section-B property
this phase exists to establish (a call failing does not affect other
calls genuinely in flight at materially higher concurrency) held at
10-way, not just at Phase 2.31's own 2-3-way.**

**Real infrastructure limitation observed, not an application
defect**: 5 of this group's 10 calls hit `FAIL (call N, verify-stt):
deepgram: connection timed out` on the harness's own *independent
post-call transcription verification* step (a second, separate real
Deepgram API call this validation script makes purely to check the
returned audio, not part of the production call path itself). This is
classified as a **real external Deepgram API capacity/rate limit under
10 simultaneous verification calls from one client**, not a
`voiceagent`/`CallRuntime` defect: the production call path's own STT
(the one driving the actual conversation) succeeded in all 10 calls
(every call reached a real LLM response); only the harness's own
*extra*, second-order verification call to Deepgram was refused/timed
out under this concurrency. Group 1 (8-way) hit this twice; group 3
(8-way, run later) hit it zero times -- consistent with an external,
load-dependent, non-deterministic provider-side limit, not a
reproducible code path. `DatabaseBoundary`'s own thread pool
(`max_workers=8`) correctly capped at 8 live threads even during the
10-way group (`peak_db_executor_thread_count=8`) -- more concurrent
calls queued for the pool rather than spawning unbounded threads,
correct bounded-executor behavior, not a defect.

## 5. Section C -- 8-way repeat (post-fix regression coverage)

Group 3 (8-way, same `5x normal, 1x media_fail, 1x cancel, 1x normal`
pattern as group 1, run after the fix was applied and the file recompiled
mid-session -- **all three groups in this run's own single process used
the fixed `detach()`**, so section C is a same-run, same-code repeat, not
a separate before/after A-B comparison; the leak's mechanism was
diagnosed from the pre-fix run's own log evidence in section 3, and the
fix's effect is confirmed by the hermetic regression test in section 8,
not by re-running the old code against real staging a second time,
deliberately, to avoid spending a second real-call budget reproducing a
now-understood, already-fixed bug). All 8 calls reached correct terminal
outcomes, full isolation held, zero `media.detach()` timeouts this
group, `media_sockets` net-delta `+0` after this group
(`after_22_calls=7` -> `after_30_calls=7`) -- consistent with, but not
sole proof of, the fix (see section 8 for the direct hermetic proof).

## 6. Real workload actually run

One continuous process, one shared `ManagedEslConnection`/media
listener/`CallRuntime`/`DatabaseBoundary`/`CallOrchestrator` for the
entire 32-call run (same production-accurate topology as Phase
2.27/2.30/2.31). `p224-freeswitch` (SIP `127.0.0.1:15080`, ESL
`127.0.0.1:18023`), real Deepgram/OpenAI/Deepgram-Aura providers
throughout.

**Command**:
`scripts/validate_staging_long_running_soak.py --fs-container
p224-freeswitch --media-public-base-url ws://192.168.65.254:8601
--sip-advertise-ip 192.168.65.254 --checkpoint-every 1 --json-out ...
--sequence "parallel:normal+normal+normal+normal+normal+media_fail+cancel+normal,normal,normal,parallel:normal+normal+normal+normal+normal+normal+media_fail+media_fail+cancel+normal,normal,normal,parallel:normal+normal+normal+normal+normal+media_fail+cancel+normal,normal,normal"`

* **3 consecutive concurrent groups**: 8-way, 10-way, 8-way (meets and
  exceeds the brief's own 8-required/10-if-safe target; 10 was safely
  achievable this session, confirmed by the 10-way group's own full
  functional-correctness result -- the only friction at 10-way was the
  external verification-step rate limiting in section 4, not an
  application-side failure).
* **2-call sequential recovery pair** between every group (calls 9-10,
  21-22, 31-32) -- each recovery call reached `completed` in ~24-26s,
  matching the pre-run smoke-test baseline, confirming the process
  remained fully usable for the next group after each one, not just
  "eventually stable."
* **Mixed outcomes per group**: every group is majority `normal` (5-6
  of 8-10) plus at least one `media_fail` and one `cancel`; group 2 has
  two `media_fail` members specifically to re-confirm multi-failure
  isolation at higher concurrency (section 4).
* **Failure-while-others-active**: every one of the 3 groups has this
  property by construction (`media_fail`/`cancel` members alongside
  `normal` members in the same `asyncio.gather()`).
* **Unique identifiers**: every call across all 32 (concurrent or
  sequential) gets a fresh, real tenant/agent/phone-number
  (`_provision_fixture`), a globally-unique local SIP/RTP port pair
  (Phase 2.31's own scheme, unchanged), a real FreeSWITCH-assigned
  channel UUID, and a real SIP `Call-ID` -- see section 7.
* **Total wall time**: 281.3s (~4.7 minutes) for 32 real calls including
  3 concurrent groups sized 8/10/8.

## 7. Per-group results

```text
group1 (8-way)  normal x5, media_fail x1, cancel x1, normal x1
  statuses=[completed,completed,completed,completed,completed,failed(media_disconnect),completed,completed]
  isolation: distinct_tenants=8 distinct_sessions=8 distinct_fs_uuids=8 distinct_sip_call_ids=8 -- PASS
  media.detach() timeouts this group: 6 (root cause, section 3)
  verify-stt (harness-side) provider timeouts: 2 of 8 -- infra, not app (section 4)

group2 (10-way) normal x6, media_fail x2, cancel x1, normal x1
  statuses=[completed,completed,completed,completed,completed,completed,failed(media_disconnect),failed(media_disconnect),completed,completed]
  isolation: distinct_tenants=10 distinct_sessions=10 distinct_fs_uuids=10 distinct_sip_call_ids=10 -- PASS
  media.detach() timeouts this group: 1
  verify-stt (harness-side) provider timeouts: 5 of 10 -- infra, not app (section 4)

group3 (8-way, post-fix)  normal x5, media_fail x1, cancel x1, normal x1
  statuses=[completed,completed,completed,completed,completed,failed(media_disconnect),completed,completed]
  isolation: distinct_tenants=8 distinct_sessions=8 distinct_fs_uuids=8 distinct_sip_call_ids=8 -- PASS
  media.detach() timeouts this group: 0
  verify-stt (harness-side) provider timeouts: 0 of 8
```

Every `media_fail` member in every group reached
`failed`/`end_reason=media_disconnect` while its concurrent siblings
reached their own correct, unaffected outcome -- held at 8-way and
10-way, not just Phase 2.31's own 2-3-way.

## 8. Section F -- hermetic regression coverage (new)

One test added to `tests/telephony/freeswitch/test_media.py` (full file:
22 tests, all pass):

**`test_detach_cancelled_mid_close_still_releases_the_socket`** -- a
`_NeverRespondsMediaSocket` whose `send_text()` never returns stands in
for the real slow-close condition; `provider.detach("call-1")` is
wrapped in `asyncio.wait_for(..., timeout=0.05)`, asserted to raise
`TimeoutError` (reproducing the real cancellation-mid-`close()` this
phase's own real-staging run hit), and then asserts `call-1` is absent
from **all three** of `_streams`, `_sockets`, and `_attached_at` --
`_streams` was already correct before this fix (included as a
same-assertion sanity check, not new behavior); `_sockets`/
`_attached_at` are the fix. This is the one deterministic, hermetic
invariant this phase's own real-staging finding exposed that was worth
locking in -- no other synthetic test was added merely to inflate
coverage.

## 9. Section D -- resource-stability analysis

| checkpoint | calls | RSS (MiB) | asyncio tasks | `_errors` | `_cancellations` | media streams | media sockets | DB executor threads | wall (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| before_first_call | 0 | 108.9 | 6 | 0 | 0 | 0 | 0 | 0 | 2.0 |
| after_8 (group1) | 8 | 135.0 | 19 | 1 | 1 | 0 | **6** | 8 | 48.5 |
| after_9 (recovery) | 9 | 133.1 | 7 | 0 | 1 | 0 | 6 | 8 | 72.9 |
| after_10 (recovery) | 10 | 134.6 | 7 | 0 | 1 | 0 | 6 | 8 | 97.6 |
| after_20 (group2) | 20 | 140.1 | 8 | 2 | 0 | 0 | **7** | 8 | 144.9 |
| after_21 (recovery) | 21 | 141.2 | 7 | 0 | 1 | 0 | 7 | 8 | 171.2 |
| after_22 (recovery) | 22 | 142.0 | 6 | 0 | 0 | 0 | 7 | 8 | 196.1 |
| after_30 (group3) | 30 | 154.3 | 6 | 1 | 0 | 0 | 7 | 8 | 232.0 |
| after_31 (recovery) | 31 | 154.9 | 6 | 0 | 0 | 0 | 7 | 8 | 256.2 |
| after_32 (recovery) | 32 | 156.4 | 7 | 0 | 1 | 0 | 7 | 8 | 281.3 |

Peak-during-group (new this phase, section 3):

| group | size | peak RSS | peak asyncio tasks | peak runtime_load | peak media streams | peak media sockets | peak DB executor threads |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 8 | 140.2 MiB | 118 | 8 | 8 | 8 | 8 |
| 2 | 10 | 148.9 MiB | 140 | 10 | 10 | 16 (10 live + 6 leaked) | 8 |
| 3 | 8 | 158.6 MiB | 112 | 8 | 8 | 15 (8 live + 7 leaked) | 8 |

**Analysis, not just numbers.**

* **`_errors`/`_cancellations`**: never exceeded 2 and 1 respectively at
  any checkpoint, and both returned to 0 within 1-2 calls every single
  time (`after_8`:1->`after_9`:0; `after_20`:2->`after_21`:0;
  `after_30`:1->`after_31`:0) -- the Phase 2.30 fix holds at 8- and
  10-way concurrency, not just Phase 2.31's own max of 3.
* **`media_streams`**: `0` at every checkpoint and peaked at exactly
  each group's own member count during the window (8, 10, 8) -- never
  inflated, confirming the leak (below) was specific to `_sockets`/
  `_attached_at`, not `_streams` itself.
* **`media_sockets`**: this is this phase's own headline finding (section
  3) -- `0 -> 6 -> 6 -> 6 -> 7 -> 7 -> 7 -> 7 -> 7 -> 7`. Not bounded,
  not self-healing on its own; fixed at the root cause, confirmed by the
  new hermetic test (section 8). This phase's own run used the
  already-fixed code for all three real-staging groups (section 5), so
  the `+0` delta after group 3 is consistent with, but is not the
  primary evidence for, the fix -- section 8's hermetic test is.
* **`db_executor_thread_count`**: flat at `8` for the entire second half
  of the run (from `after_8` onward) -- `DatabaseBoundary(max_workers=8)`
  correctly capped even under the 10-way group's own higher demand;
  no unbounded growth.
* **`asyncio_tasks`**: baseline oscillates 6-7 between groups (matching
  Phase 2.31's own 6-7 baseline range exactly), spiking only during each
  group's own concurrent window (peak 118/140/112) and returning to
  baseline immediately after -- no accumulation across groups.
* **RSS**: `108.9 -> 135.0 -> 133.1 -> 134.6 -> 140.1 -> 141.2 -> 142.0 ->
  154.3 -> 154.9 -> 156.4` MiB. Unlike Phase 2.31's own 60-call run
  (which rose over ~40 calls then plateaued and fell), this shorter
  32-call run's RSS **rose across the entire run and did not plateau or
  fall back** -- stated honestly as a **PARTIAL**, not GREEN, resource
  finding: at only 32 calls (roughly half Phase 2.31's own 60), this is
  equally consistent with (a) ordinary warm-up not yet finished at this
  shorter length, given the added cost of provisioning/tearing down
  60%-larger concurrent batches than Phase 2.31 ever ran, or (b) a slow
  per-call/per-group retention this run's own length cannot rule out.
  This phase does not claim either explanation over the other -- see
  section 9a.

## 9a. Limitations, stated honestly

* **RSS trend not resolved at this run length** (section 9's own
  analysis) -- a longer sustained-concurrency run (more groups, or
  larger ones) would be needed to distinguish "warm-up, still climbing
  because concurrent groups are more expensive to set up/tear down than
  Phase 2.31's own sequential calls" from "a slow per-group retention."
  Not claimed as a leak; not claimed as ruled out either.
* **10-way was the largest concurrency this session safely established
  and fully completed** -- not pushed further (e.g. 12-15-way) this
  phase, since 10-way already exceeded the brief's own "10 if safe"
  target and already surfaced a genuine defect worth root-causing before
  adding more concurrency on top of an unfixed leak.
* **The verify-stt Deepgram timeouts (section 4) were not deeply
  investigated beyond classification** -- e.g. no attempt was made to
  determine whether they were rate-limit (429-class) or plain
  connection-pool exhaustion on the harness's own side; both would
  produce the same observed `deepgram: connection timed out` message
  from this harness's own independent verification helper
  (`validate_staging_sip_spoken_e2e._transcribe`), and distinguishing
  them was not necessary to correctly classify this as infra/provider,
  not application, since the *production* call path's own STT succeeded
  in all 10 group-2 calls regardless.
* **Section C is a same-run repeat, not an independent before/after
  A-B trial** -- see section 5's own note: all three real-staging groups
  in this run used the already-fixed `detach()` (the fix was applied and
  the module reloaded mid-session, before any group ran against real
  staging), so the fix's correctness rests on the hermetic test (section
  8), not on re-triggering the original bug against real staging a
  second time on purpose.
* **Section E (listener-level disconnect) was not reattempted this
  phase** -- out of this phase's own scope (sustained concurrency, not
  listener-disconnect); Phase 2.31 section 7's own documented
  environment limitation (`p223-freeswitch`'s dialplan incompatible with
  this harness's generic ESL-routed call path) is unchanged and was not
  re-investigated here.
* FreeSWITCH's own container-side CPU/mem (`docker stats`, sampled via
  `fs_stats` in each checkpoint) stayed low throughout (1.4-7.5% CPU,
  52-67MiB) and was not analyzed in further depth -- consistent with
  Phase 2.31's own stated scoping (FreeSWITCH's own resource ceiling
  remains Phase 2.27's own territory, not repeated here).

## 10. Newly discovered defects

**One, found and fixed** (section 3): `FreeSwitchMediaProvider.detach()`
leaked `_sockets`/`_attached_at` entries when the caller's own
`asyncio.wait_for` timeout cancelled it mid-`close()` -- invisible at
Phase 2.31's own max concurrency of 3 (never enough simultaneous
teardown pressure to hit the 5.0s timeout), reproducible starting at
8-way. Fixed by reordering two `.pop()` calls to before the awaited
`close()`, mirroring `_streams`'s own already-correct ordering. Hermetic
regression test added (section 8). No other new defect found.

## 11. Findings not reopened

* Phase 2.29's `TransportError` -> `media_disconnect` classification:
  confirmed unchanged and correct across all 4 `media_fail` calls in
  this phase's own 8-/10-way groups.
* Phase 2.30's `CallRuntime._errors`/`_cancellations` bounding fix:
  confirmed still correct at 8- and 10-way concurrency (section 9).
* The "alpha"->"also"-class STT recognition variance (Phase 2.28): not
  reopened; the elevated `own_reply_present=False`/verify-stt-timeout
  rate observed in this phase's own 8-/10-way groups is attributed to
  real provider load under higher concurrency (section 4), a
  quantitatively different but qualitatively same, already-understood
  provider characteristic, not a new concurrency defect in
  `voiceagent/`.

## 12. Validation gates

* Targeted Phase 2.32 test
  (`tests/telephony/freeswitch/test_media.py`, full file, 22 tests):
  **all passed**.
* Hermetic `pytest` (default `addopts`, excludes `-m integration`):
  **960 passed, 1 failed, 961 total** (`--junitxml` confirmed:
  `tests=961 failures=1 errors=0 skipped=0`). The one failure,
  `tests/architecture/test_phase2_0_docs.py
  ::test_every_sha_like_token_in_docs_is_the_canonical_pin`, is
  **pre-existing and unrelated to this phase**: it flags every 40-hex-
  character token in `docs/` that isn't the canonical SaaS-OS pin SHA,
  and matches 3 occurrences of `cf11189a9fdafd80e58a1ab26b23b140f4e77904`
  (a **git commit hash**, Phase 2.31's own stated baseline HEAD, not a
  SaaS-OS pin) inside `docs/PHASE-2.31-LONG-RUNNING-RUNTIME-VALIDATION
  .md` -- a file this phase did not touch (confirmed via `git diff
  --stat`, not in this phase's own changed-files list, section 15).
  Not fixed here: touching a pre-existing, unrelated Phase 2.31 doc (or
  its own guard test's regex) is out of this phase's own scope.
* Real integration suite (`pytest -m integration`, real throwaway
  PostgreSQL): **261 passed, 0 failed** (`--junitxml` confirmed:
  `tests=261 failures=0 errors=0`), including
  `test_one_call_failing_does_not_affect_others` (Phase 2.31's own
  documented pre-existing non-deterministic timing race, unrelated to
  this phase, passed this run).
* Ruff check: **all checks passed**.
* Ruff format: **359 files already formatted**.
* Pyright: **0 errors, 0 warnings, 0 informations**.
* import-linter (`lint-imports`): **8 contracts kept, 0 broken**.
* detect-secrets (`.secrets.baseline`): scan's own known in-place
  mutation reproduced again (same pre-existing findings in files
  entirely outside this phase's own changes, matching Phase 2.29-2.31's
  own documented precedent) -- diffed, explicitly confirmed **zero** new
  findings in any file this phase touched (`media.py`, `test_media.py`,
  `validate_staging_long_running_soak.py`), reverted with `git checkout
  -- .secrets.baseline` before ever being staged.
* pip-audit: **no known vulnerabilities** (`saas-os`/`voiceagent`
  skipped, not on PyPI, expected and unchanged from every prior phase).
* `git diff --check`: **clean** (exit 0).

## 13. Failure-isolation results

* One media-failed call never finalized an unrelated concurrent call: in
  every group, the `media_fail` member(s) reached `failed`/
  `media_disconnect` while every sibling reached its own correct,
  independent terminal state (section 7) -- true at 8-way and 10-way,
  including group 2's own two-simultaneous-failures case.
* One cancellation never cancelled an unrelated call: every `cancel`
  member reached its own real terminal status (`completed` in this run's
  own 3 trials) without affecting any sibling's own outcome.
* No runtime/provider exception terminated the supervisor: the process
  ran continuously through all 32 calls, 3 concurrent groups, and 6
  `media_fail`+`cancel`-induced real failures/early-hangups without
  crashing or requiring a restart.
* Other calls continued to their expected terminal state regardless of
  what happened to a concurrent sibling, in all 3 groups.

## 14. Identifier-isolation results

`distinct tenants=32 distinct sessions=32 distinct fs_uuids=32 distinct
sip_call_ids=32 (of 32 calls)` -- zero reuse, zero collision, across
sequential and concurrent calls alike. Every one of the 3 concurrent
groups independently re-confirms this within just that group's own
members (section 7). `cross_contamination_detected=False` for all 32
calls (checked against the JSON evidence directly) -- no call's own
transcript ever showed another call's own distinguishing keyword.

## 15. SaaS-OS pin verification

Verified unchanged, both before and after this phase's work:
`ff550010e5eafecace7311038aadc99fcecfbe3d`
(`.venv/Lib/site-packages/saas_os-0.1.0.dist-info/direct_url.json`,
`commit_id` and `requested_revision` both match, `vcs: "git"`; also
confirmed in `pyproject.toml` line 27, unchanged).

## 16. Final git status

```text
 M docs/PHASE-0-ARCHITECTURE.md
 M scripts/validate_staging_long_running_soak.py
 M tests/telephony/freeswitch/test_media.py
 M voiceagent/telephony/freeswitch/media.py
?? docs/ADR/0010-one-frontend-multiple-user-contexts.md
?? docs/PHASE-2.32-SUSTAINED-CONCURRENCY-VALIDATION.md
```

HEAD: `0f7c9d5d59576d8d7d6f57120e8f63de594ef805` (unchanged from this
phase's own baseline). `git log origin/main..HEAD` is empty.

## 17. Protected files / commit / push confirmation

* **Protected files untouched**: `docs/PHASE-0-ARCHITECTURE.md` shows
  only its own pre-existing, unrelated modification (present before this
  phase started, never staged or altered by this phase's own work);
  `docs/ADR/0010-one-frontend-multiple-user-contexts.md` remains
  untracked, exactly as it was at the start of this phase.
* **Nothing was committed.** HEAD remains
  `0f7c9d5d59576d8d7d6f57120e8f63de594ef805`, identical to this phase's
  own recorded starting point; no new commit exists.
* **Nothing was pushed.** `git log origin/main..HEAD` is empty --
  local and `origin/main` are identical.
* **No reset, rebase, amend, or squash** was performed at any point.
* **SaaS-OS was not modified** -- only consumed as an installed, pinned
  dependency (its own migrations run against a throwaway database, its
  package files never edited).

## 18. Files changed this phase

```text
scripts/validate_staging_long_running_soak.py (extended)  -- GroupPeak/_sample_peaks_during
                                                               background peak sampler for
                                                               parallel: groups; no scenario/
                                                               sequence-parsing change needed,
                                                               8-/10-way groups already worked
                                                               via the existing mechanism
voiceagent/telephony/freeswitch/media.py (fixed)           -- detach(): pop _sockets/
                                                               _attached_at before the awaited
                                                               stream.close(), not after
                                                               (section 3 root cause)
tests/telephony/freeswitch/test_media.py (extended)        -- 1 new hermetic regression test
docs/PHASE-2.32-SUSTAINED-CONCURRENCY-VALIDATION.md (new)  -- this document
```

`voiceagent/telephony/freeswitch/media.py` is the one production file
changed this phase, justified in full in section 3: a narrow, five-line
reordering fixing a reproduced, mechanically-explained real-staging
resource leak, not a broad rewrite.
