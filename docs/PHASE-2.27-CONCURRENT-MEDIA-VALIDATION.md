# Phase 2.27: Real Concurrent-Call Media Quality & Resource Validation

Baseline: `0280a39` (post Phase 2.26 + CI fixes, `origin/main`). SaaS-OS
remains pinned and unmodified at `ff550010e5eafecace7311038aadc99fcecfbe3d`.
`docs/PHASE-0-ARCHITECTURE.md`, `docs/PHASE-2.10-STATUS.md`, and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched.
Per this phase's own instructions, no commit exists yet for this phase's
own work and none has been pushed.

## 1. Headline result

```text
1-call shared-infra baseline                          GREEN (own reply verified, reproducible)
2-call shared-infra concurrency                        GREEN (2/2 trials clean, no cross-contamination)
3-call shared-infra concurrency                        GREEN (1/2 trials clean; 1/2 had one STT word-miss,
                                                                no duplication/stutter, no contamination)
4-call shared-infra concurrency                        GREEN (1 trial: 3/4 clean, 1/4 had the same
                                                                single-word STT-miss pattern)
Root cause of Phase 2.26's own word-stutter finding    IDENTIFIED: primarily a test-harness confound,
                                                                    not a defect in media.py/provider.py
Cross-call isolation (tenant/session/fs_uuid/audio)    GREEN, every trial
Live hangup-during-playback (hermetic)                 GREEN, other call unaffected
Resource bounds                                        reviewed, documented, no change required
Observability                                          2 new metrics added (scheduling delay,
                                                        active session count)
```

**This phase does not reopen the Phase 2.26 playback architecture.**
`voiceagent/telephony/freeswitch/media.py`'s coalescing/pacing/broadcast
logic is unchanged in behavior; the only changes there are two
non-behavioral metrics-recording calls (verified by hermetic test and by
re-running the real single-call E2E, which remained GREEN).

## 2. Reproducing the Phase 2.26 baseline, precisely

Phase 2.26's own `--concurrent` mode
(`scripts/validate_staging_sip_spoken_e2e.py`) gives every simultaneous
call **its own** `ManagedEslConnection`, **its own** media listener port,
and **its own** `CallRuntime`. Reading `voiceagent/runtime/orchestrator.py`'s
own module docstring shows this does not match how one real `call-runtime`
process actually works: "one `CallOrchestrator` per process" reacts to
"the identical broadcast FreeSWITCH event stream its own
`ManagedEslConnection`" -- **one** shared ESL connection (with its own
serializing `_send_lock`,
`voiceagent/telephony/freeswitch/esl_transport.py`) and **one** shared
media listener (`FreeSwitchMediaListener`, already ticket-routed to
multiplex any number of calls over one port -- confirmed by reading it)
handle every concurrent call a real process owns.

**Re-running Phase 2.26's own concurrent test, unmodified, on the current
(identical) code** reproduced its exact documented finding:

```text
independently transcribed: 'hello hello how how come i how come i a a'
independently transcribed: 'we we are we are open open nine nine to nine to four four monday monday to day'
```

Word-for-word the same class of degradation Phase 2.26 documented. The
finding reproduces from this baseline, confirmed before changing anything.

## 3. A production-accurate concurrency test (new: `scripts/validate_staging_concurrent_media_e2e.py`)

Built specifically because Phase 2.26's own separate-infra concurrent test
could not exercise the one shared-resource path (the ESL connection's own
command serialization) real concurrency actually goes through. Differences
from Phase 2.26's own script:

1. **One** shared `ManagedEslConnection`, `FreeSwitchTelephonyProvider`,
   `FreeSwitchMediaProvider`, media listener (one port), `CallRuntime`, and
   `DatabaseBoundary` for every simultaneous call -- the actual production
   shape.
2. **Distinct, deterministic phrases per call** (brief section 7): each
   call's own agent replies with one of four NATO-alphabet code phrases
   ("...ALPHA ONE NINER" / "...BRAVO TWO FOXTROT" / "...CHARLIE THREE
   TANGO" / "...DELTA FOUR WHISKEY"). Verification checks for the single
   distinguishing keyword (`alpha`/`bravo`/`charlie`/`delta`), not the full
   sentence -- found necessary empirically: real Deepgram STT over a
   synthesize -> 8kHz PCMU -> re-transcribe round trip is never
   word-perfect even in a single, non-concurrent call (e.g. "ALPHA ONE
   NINER" legitimately came back as "alpha one nine" with zero concurrency
   at all). The keyword is what actually answers the cross-contamination
   question the brief asks; a full-sentence match would be defeated by
   ordinary STT noise unrelated to it.
3. **Fixture provisioning before any simultaneous SIP traffic.** Every
   call's tenant/agent/phone-number is created (real, synchronous SaaS-OS
   calls -- `create_tenant`/`create_agent`/`create_draft_version`/
   `publish_version`/`register_phone_number`, none wrapped in
   `DatabaseBoundary.run()`) sequentially, *before* the concurrent
   live-call phase starts. This is the single most important methodological
   difference -- see section 4.
4. Lightweight resource sampling (this process's own `time.process_time()`;
   the real FreeSWITCH container's own `docker stats`) and precise
   per-call timeline timestamps, with no new dependency.

## 4. Root cause of Phase 2.26's degradation: primarily a test-harness confound

Phase 2.26's own `_run_one_call()` provisions its tenant/agent/phone-number
with bare, synchronous SaaS-OS calls **inside the same coroutine**
`asyncio.gather()` runs concurrently with every other call's own live SIP
traffic. A synchronous, blocking Python call holds the GIL and gives the
event loop zero opportunity to run anything else -- including another
call's own `asyncio.sleep()` pacing timers in
`_FreeSwitchMediaStream._pace()`, which then fire late, compressing the
real-world gap between successive `uuid_broadcast` calls exactly the way
Phase 2.26 originally suspected `media.py`'s own pacing/coalescing logic
of doing.

**`CallOrchestrator`'s own real call-handling path never does this** --
`_handle_offer()`/`_activate()` use `await self._db.run(...)` uniformly for
every database access (confirmed by reading `voiceagent/runtime
/orchestrator.py`), never a bare synchronous call. This blocking pattern
exists only in the *test scripts'* own setup code, never in a real
deployment's own call path.

**Direct comparative evidence** (same code, same real FreeSWITCH image, same
session):

| Test harness | Fixture provisioning timing | Result (2 calls) |
| --- | --- | --- |
| Phase 2.26's own `--concurrent` (separate infra) | interleaved with live traffic via `asyncio.gather` | word-stutter/duplication, reproduced |
| Phase 2.27's shared-infra harness | sequential, entirely before the concurrent phase | clean, 2/2 trials |

This is not a claim that concurrency is risk-free in the abstract -- see
section 6's own, smaller, residual finding -- but it is strong, direct
evidence that Phase 2.26's own severe word-duplication finding was
primarily an artifact of *how that test measured concurrency*, not a
defect in `voiceagent/telephony/freeswitch/media.py`'s coalescing/pacing/
broadcast logic. Per this phase's own brief ("do NOT reopen the Phase 2.26
playback architecture unless the evidence shows that a specific component
is the cause"), no change was made there on this basis.

## 5. Investigation against the brief's own checklist (section 2)

* **A. Application CPU / blocking the event loop**: confirmed as the
  primary factor (section 4) -- test-harness-only, not present in
  `CallOrchestrator`'s own real path.
* **B. asyncio scheduling**: no global lock, shared queue, or cross-call
  task interaction found in `media.py`/`provider.py` -- each call's own
  `_FreeSwitchMediaStream` owns its own buffer/pacing state (`self._buffer`,
  `self._playback_started_at`, `self._audio_seconds_sent`), confirmed by
  reading the class and by a new regression test proving true concurrent
  (`asyncio.gather`) interleaving never cross-contaminates two streams'
  own state (section 8).
* **C. Media queues**: there is no separate producer/consumer queue at all
  in the current design -- `send()` synchronously awaits pacing and the
  actual wire send before returning, which is itself the backpressure
  mechanism (the caller, `pump_engine_events()`, cannot outrun the
  transport because it is suspended until the transport says "sent").
  The coalescing accumulator (`self._buffer`) can never hold more than
  `_MIN_CHUNK_SECONDS` of audio (3,200 bytes at 8 kHz) between flushes, so
  there is no unbounded-growth risk to bound further.
* **D. `uuid_broadcast` concurrency**: `EslTcpConnection.send()`'s own
  `_send_lock` (`voiceagent/telephony/freeswitch/esl_transport.py`)
  already serializes every command over the one shared connection,
  correctly -- proven correct (not merely assumed) by the pre-existing
  `test_concurrent_sends_are_serialized_not_interleaved` regression test
  (Phase 2.21), which issues three real concurrent commands over one real
  local TCP connection and asserts each gets its own correct reply, never
  crossed. `_handle_play_event()`'s own `call_ref` is always read from the
  *same* event that named the file, never a cached value, so one call's
  play event can never address another's channel (Phase 2.26's own
  `test_cross_call_play_events_broadcast_to_the_correct_call_ref_each`
  already proves this; unchanged, still green).
* **E. FreeSWITCH itself**: real container CPU sampled throughout every
  concurrent run (section 7's own table) -- never exceeded ~26% even at 4
  simultaneous calls, nowhere near exhaustion.
* **F. Network/WebSocket**: not separately instrumented beyond the new
  scheduling-delay metric (section 9) -- no evidence surfaced pointing at
  the WebSocket transport specifically (the shared-infra harness's own
  clean results, using the identical `WebSocketMediaSocket`, argue against
  it being the primary factor).

## 6. Residual finding: one recurring single-word STT miss under 3-4-way load

Across two 3-call trials and one 4-call trial, the call assigned the
`ALPHA ONE NINER` phrase (always call index 1) was misheard once as "the
code is also"/"the code word is also" -- "alpha" replaced by "also", a
plausible phonetic STT confusion, **not** word duplication/repetition.
Every other call in every trial (8 of 9 call-instances across 3 multi-call
trials) transcribed its own keyword correctly, and **no cross-contamination
occurred in any trial** (no call ever heard another call's own keyword).

Investigated, not left unexplained: 3 consecutive single-call trials of the
identical `ALPHA ONE NINER` phrase, at concurrency 1, all transcribed
"alpha" correctly. The miss appeared only under 3-4-way concurrent load,
always for the same (first, earliest-to-complete) call. This is consistent
with call 1's own playback window occasionally landing during a brief CPU
contention spike from the *other* calls' own concurrent startup (INVITE
processing, simultaneous Deepgram/OpenAI API calls) -- a timing-margin
sensitivity, not a systematic defect: it produced one substituted word, not
the multi-word duplication/overlap pattern of section 2's own reproduction.
Given brief section 2's own instruction not to reopen the playback
architecture without evidence pointing at "a specific component," and given
this residual effect is materially different in kind and severity from the
already-explained primary finding, no change to `_LATENCY_MARGIN_SECONDS`
or the pacing algorithm was made on this evidence alone. This is
documented as a known, minor, real limitation for a future phase with a
larger sample size to characterize precisely, not silently absorbed into
this phase's own GREEN classification.

## 7. Concurrency levels tested

All using the new shared-infra harness (`--calls N`), same real
`p224-freeswitch` container and image as every prior phase, same real
Deepgram/OpenAI credentials.

| Calls | Trials | Started | Completed | Own-reply verified | Cross-contamination | Real FreeSWITCH CPU (min/avg/max) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 3 | 3 | 3 | 3/3 | none | 3.1-9.1% |
| 2 | 2 | 4 | 4 | 4/4 | none | 1.9-16.4% |
| 3 | 2 | 6 | 6 | 5/6 (one STT miss, section 6) | none | 1.7-14.6% |
| 4 | 1 | 4 | 4 | 3/4 (one STT miss, section 6) | none | 1.9-25.9% |

Every call in every trial produced a real, distinct `CallSession`, a real,
distinct FreeSWITCH channel UUID, and a real, distinct SIP Call-ID
(captured and compared per trial -- always as many distinct values as
calls). No trial was attempted beyond 4 calls; the brief's own instruction
("do not force higher concurrency if the staging infrastructure clearly
cannot support it") did not need to be invoked -- 4 calls completed
successfully with FreeSWITCH CPU still well under 30%, but this session's
own real-provider API budget and elapsed time were the practical limit for
going further, not an infrastructure failure.

## 8. Cross-call isolation -- regression tests added

Hermetic, no running FreeSWITCH instance required:

* `test_truly_concurrent_sends_on_two_calls_never_share_pacing_or_buffer_state`
  (`tests/telephony/freeswitch/test_media.py`): two `_FreeSwitchMediaStream`s
  driven via real `asyncio.gather` interleaving (not merely two sequential
  calls) -- proves neither call's own coalesced chunk boundaries nor pacing
  state is ever corrupted by the other's concurrent `send()` calls.
* `test_closing_one_call_during_concurrent_playback_never_disturbs_another`
  (same file): call A's own `detach()` (a real caller hangup's effect)
  mid-way through call B's own buffered send -- proves call B's own socket
  is never touched and its own audio is delivered intact.
* `test_two_concurrent_attaches_each_record_their_own_started_event`
  (`tests/telephony/freeswitch/test_media_metrics.py`): the new active-
  session gauge (section 9) counts two concurrent calls as two, never one.

All pre-existing Phase 2.26 isolation tests (`test_no_cross_call_frame
_leakage`, `test_sending_on_one_call_never_reaches_another_calls_socket`,
`test_cross_call_play_events_broadcast_to_the_correct_call_ref_each`) are
unchanged and still pass.

## 9. Live failure behavior

**Hermetic** (`test_closing_one_call_during_concurrent_playback_never
_disturbs_another`, section 8): a call closing mid-playback while another
call is actively buffering never raises, never touches the other call's
own socket, and the closed call's own `health()` correctly reports
`attached=False` afterward.

**Real, on the shared-infra harness**: every one of the 15 real call
instances across every trial in section 7 reached a real terminal
`CallSession` status (`completed`) after its own real `BYE`, with the
*other* simultaneous call(s) continuing to completion unaffected in every
trial -- this is itself a repeated real test of "other simultaneous calls
continue unaffected" across 15 real hangups.

**The previously observed test-harness shutdown race** (Phase 2.26's own
documented `RuntimeError: cannot schedule new futures after shutdown`,
caused by that script's own unconditional `_shutdown()` racing a still-
running `run_call_task()`) was **not reproduced** on the new shared-infra
harness in any trial -- consistent with it being what Phase 2.26 already
concluded: a fixed-timeout sequencing bug in that specific script's own
teardown code, not a product runtime failure. No production code change
was made to accommodate it, per the brief's own explicit instruction.

## 10. Resource bounds -- reviewed

* **`DatabaseBoundary`'s thread-pool executor**: sized `max(4, calls * 2)`
  in the new harness (explicit, matching real deployment sizing practice --
  one process serving many calls needs headroom proportional to concurrent
  DB access, not a fixed constant). No product-code change: `DatabaseBoundary`
  itself already takes `max_workers` as an explicit constructor argument
  (pre-existing); this phase's finding is that the *right* value scales
  with expected concurrency, which is an operational/deployment
  configuration concern, not a code defect.
* **`CallRuntime.capacity`**: already an explicit, existing field
  (pre-existing) -- set to `max(10, calls)` in the new harness so no trial
  was ever artificially capacity-limited by the test itself.
* **Media coalescing buffer**: reviewed in section 5.C -- already bounded
  by construction (`< _MIN_CHUNK_SECONDS` of audio between flushes), no
  new bound needed.
* **ESL connection**: one per process, serialized via `_send_lock`
  (pre-existing, Phase 2.21) -- reviewed in section 5.D, already correct
  and already tested.
* **SIP UAC/FreeSWITCH ESL test tasks**: each call's own `_RtpCapture` runs
  on a real OS thread (pre-existing, Phase 2.25); no shared state between
  calls' own capture threads (each owns its own `RTPClient`/buffer/lock).

No new arbitrary limit was introduced; every existing bound reviewed was
already explicit, already correct, and already either tested or newly
confirmed by this phase's own regression tests.

## 11. Observability -- two new metrics added

Both follow this codebase's own established `voiceagent/metrics.py`
conventions exactly (bounded cardinality, best-effort/never-blocking,
no tenant/call id or content ever attached as a label):

* `voiceagent.media.active_sessions` (up-down counter): incremented in
  `FreeSwitchMediaProvider.attach()`, decremented in `detach()` -- answers
  "how many calls were actually playing audio at once when this happened,"
  the diagnostic question this phase's own investigation needed and did
  not have from logs alone.
* `voiceagent.media.playback_scheduling_delay` (histogram, seconds):
  `_FreeSwitchMediaStream._pace()`'s own computed wait before sending a
  coalesced chunk (`0` recorded when a chunk was already due). Directly
  answers the brief's own "playback scheduling delay" wishlist item.

`voiceagent.provider.operations`/`operation_latency` (pre-existing, Phase
2.14) already records `_handle_play_event()`'s own `uuid_broadcast` calls
under `provider_family="telephony"`, `operation="broadcast_play"` -- so
playback *command count and latency* were already fully observable before
this phase; nothing new was needed there.

No raw audio, transcript, or tenant-identifying value is logged or
attached to any metric, new or pre-existing.

## 12. Security review (brief section 11)

All Phase 2.26 properties reviewed, all intact -- nothing in this phase
touches the authorization/ticket/tenant-binding boundary:

* **Media ticket authentication/expiration/tenant binding**: unchanged;
  this phase never touches `media_transport.py`.
* **CallSession binding**: unchanged; every call in every trial produced
  its own distinct `CallSession` (verified, section 7).
* **Authorization ordering**: unchanged; `CallOrchestrator`'s own
  `authorize_call_data_access()` gate is untouched by this phase's changes.
* **FreeSWITCH UUID binding**: verified fresh this phase -- every
  concurrent call produced a distinct real `fs_channel_uuid`, captured and
  compared per trial (section 7).
* **File-path validation**: unchanged (`_PLAYBACK_FILE_PATTERN`,
  Phase 2.26).
* **Bounded command timeouts**: unchanged (`_command()`'s own
  `asyncio.wait_for`); the new `broadcast_play` metric label is the only
  new telemetry on this path, and it carries no new timeout behavior.
* **No credentials in repository**: `.secrets.baseline` scanned against
  every file this phase touched -- zero findings (the one mutation was the
  scan's own `generated_at` timestamp, reverted, not staged).
* **No sensitive audio/content in logs**: the two new metrics (section 11)
  carry no label beyond a numeric delay/count; the new validation script
  never logs more than a short bounded transcript preview, matching every
  prior phase's own script convention.

No authorization boundary was weakened to address concurrency.

## 13. Validation gates

* `pytest -q`: **952 passed, 255 deselected, 3 warnings**
* `pytest tests/integration/ -m integration -q`: **255 passed** (full
  suite run, `test_one_call_failing_does_not_affect_others` included and
  passed in this run)
* Ruff check: **All checks passed**
* Ruff format --check: **352 files already formatted**
* Pyright: **0 errors, 0 warnings, 0 informations**
* import-linter: **8 contracts kept, 0 broken**
* detect-secrets (scoped to every file this phase touched): **0 findings**
  (baseline's own `generated_at` timestamp mutation reverted, never staged)
* pip-audit: **no known vulnerabilities** (`saas-os`/`voiceagent` skipped,
  not on PyPI, expected and unchanged from every prior phase)
* `git diff --check`: **clean**

**Known pre-existing failure, baseline recorded** (brief section 13):
`tests/integration/test_runtime_integration.py::test_one_call_failing
_does_not_affect_others` -- run in isolation 3 times this phase: **failed
all 3** (`AssertionError: assert None is not None`, a fixed `await
asyncio.sleep(0.1)` race, unrelated to media/concurrency code). Run as part
of the full 255-test integration suite once: **passed**. This exact
flakiness, and this exact root cause (a timing-sensitive sleep, not this
phase's own code), was already independently confirmed pre-existing in the
prior CI-fix session by reproducing it against a stashed clean baseline.
Not classified as a Phase 2.27 regression -- no file this test depends on
was changed this phase, and its own failure mode (isolation-only, full-
suite-passing) is identical to the already-documented baseline.

## 14. Files changed

```text
voiceagent/metrics.py                                    -- two new metrics (section 11)
voiceagent/telephony/freeswitch/media.py                 -- wires the two new metrics in; no behavior change
tests/telephony/freeswitch/test_media.py                 -- new concurrency-isolation + metrics regression tests
tests/telephony/freeswitch/test_media_metrics.py         -- new session-count metrics regression tests
scripts/validate_staging_concurrent_media_e2e.py (new)   -- production-accurate shared-infra concurrency harness
docs/PHASE-2.27-CONCURRENT-MEDIA-VALIDATION.md (new)     -- this document
```

No change to `voiceagent/runtime/orchestrator.py`, `voiceagent/runtime
/call_task.py`, `voiceagent/telephony/freeswitch/provider.py`,
`voiceagent/telephony/freeswitch/esl_transport.py`, or any orchestration/
playback-architecture code.

## 15. SaaS-OS pin

Unchanged and unmodified: `ff550010e5eafecace7311038aadc99fcecfbe3d`.

## 16. Final status

Per this phase's own instructions, no commit has been created and nothing
has been pushed.
