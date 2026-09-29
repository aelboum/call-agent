# Phase 2.29: Real Media Failure & Long-Running Stability Validation

Baseline: `6227705` (post Phase 2.28, `origin/main`). SaaS-OS remains pinned
and unmodified at `ff550010e5eafecace7311038aadc99fcecfbe3d`.
`docs/PHASE-0-ARCHITECTURE.md` and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched. Per
this phase's own instructions, no commit exists yet for this phase's own
work and none has been pushed.

**Status vocabulary used throughout, exactly as the brief requires:**
category is one of **REAL STAGING** / **HERMETIC-MOCKED** / **NOT
VALIDATED** (never blurred); result is one of **GREEN** (genuinely
validated) / **PARTIAL** (some but not all requested evidence) / **NOT
VALIDATED** (could not safely establish) / **FAILED** (reproducible
defect).

## 0. Headline result

```text
A genuine, reproducible production defect was found and reported for
review, per this phase's own brief section 8 -- NOT fixed in the original
Phase 2.29 session. See section 1.

UPDATE (later, separate, reviewed and approved session): fixed. See
section 1a for the root cause, exact remediation, regression coverage,
and real-staging re-verification. Everything below this line reflects the
ORIGINAL Phase 2.29 finding, preserved as found; section 1a documents the
fix and the re-graded, now-GREEN result on top of it.
```

```text
Section 2 (per-call media disconnect)     REAL STAGING, FAILED -- see section 1/2; FIXED and re-graded GREEN, see section 1a
Section 3 (cancellation during AI/media)   REAL STAGING + HERMETIC, GREEN
Section 4A (2-call failure isolation)      REAL STAGING, GREEN (isolation); FAILED (status defect, same root cause); FIXED, see section 1a
Section 4B (3-4-call failure isolation)    REAL STAGING, GREEN (isolation); FAILED (status defect, same root cause); FIXED, see section 1a
Section 5 (soak)                           REAL STAGING, PARTIAL (small N, honestly bounded) -- unchanged by the fix
Section 6 (repeated failure cleanup)       REAL STAGING, PARTIAL (process-level, not object-level) -- unchanged by the fix
Section 7 (security)                       REAL STAGING + code review, GREEN -- unchanged by the fix
```

## 1. Production defect found -- reported for review, not fixed

**This is the headline finding of this phase. Flagged first, not buried.**

**Symptom.** When a call's own media transport dies mid-turn -- a real
`ConnectionClosedOK`/similar exception raised out of
`_FreeSwitchMediaStream.send()` (`voiceagent/telephony/freeswitch/media.py`)
-- while the SIP call has **not** received a real hangup and the outer
runtime has **not** cancelled the call, `run_call_task()`'s own `finally`
block (`voiceagent/runtime/call_task.py`) finalizes the `CallSession` as
`status="completed"`, `end_reason="completed"` -- indistinguishable from an
ordinary successful call. A genuine mid-call media failure is silently
misreported as success.

**Exact code path.**

* `voiceagent/runtime/call_task.py:141` -- `CancellationSignal.reason: str
  = "hangup"` (dataclass default).
* `voiceagent/runtime/call_task.py:252-267` -- `_final_status(reason)`
  already has correct, dedicated handling for exactly this situation:
  `if reason in ("provider_disconnect", "media_disconnect"): return
  "failed", reason` -- but **nothing in the codebase ever sets
  `cancellation.reason` to either of those two values.** They are dead
  branches. The only writer of `cancellation.reason` is
  `_watch_for_remote_hangup()` (line 437, sets it to `"hangup"` on a real
  `CallEventType.HUNGUP`) and the supervisor's own external-cancellation
  path (`voiceagent/runtime/supervisor.py:184`, also application-set
  reasons like `"runtime_shutdown"`).
* `voiceagent/runtime/call_task.py:389-449` --
  `_run_pumps_with_remote_hangup_detection()` only special-cases
  `asyncio.CancelledError` (line 444: `except asyncio.CancelledError: if
  not hangup_triggered: raise`). An **ordinary exception** (not
  `CancelledError`) escaping `_run_pumps()` -- exactly what a dead media
  transport produces, since `pump_engine_events()`
  (`call_task.py:340-343`) does not catch anything around `await
  media_stream.send(event.frame)` -- propagates straight through, past
  that `except` clause (it does not match), up into `run_call_task()`'s
  own `finally` block (line 587) with `cancellation.reason` still at its
  unchanged default, `"hangup"`.
* `voiceagent/runtime/call_task.py:628-629` -- `if not finalized: status,
  end_reason = _final_status(cancellation.reason)` then calls
  `_final_status("hangup")`, which returns `("completed", "completed")`
  (line 261-262) -- the *correct* mapping for an actual hangup, wrongly
  applied here because nothing upstream ever told it this was not one.

**Why this is a genuine defect, not a test-harness artifact.** The
`_final_status()` function's own existing `"media_disconnect"`/
`"provider_disconnect"` branches prove the original author already
anticipated this exact failure mode and intended it to map to `"failed"` --
the wiring to actually set that reason when it happens was simply never
completed. This is a gap in the product's own call-outcome reporting, with
real consequences for anything downstream that trusts `CallSession.status`
(billing, analytics, CS tooling, this repo's own future validation
scripts) to mean what it says.

**Reproduced twice, for real, against the real staging stack** (section
2's own experiment, run twice -- once at 2-call concurrency, once at
4-call concurrency): a real `api uuid_audio_stream <uuid> stop` issued
against one call's own real FreeSWITCH channel (`p224-freeswitch`, real ESL
connection) produced, both times, a real, logged
`websockets.exceptions.ConnectionClosedOK: received 1000 (OK) Normal
closure; then sent 1000 (OK) Normal closure` from `media_stream.send()`, an
ERROR-level `call.task.failed` log from `voiceagent/runtime/supervisor.py`
(proving the exception genuinely propagated all the way up, uncaught), and
in both cases the final `CallSession.status == 'completed'`,
`end_reason == 'completed'`. Full traceback (call 1 of the 2-call trial,
`fs_channel_uuid=01a0e8c8-8dcd-7e50-aa07-c01066024980`):

```text
Traceback (most recent call last):
  File ".../voiceagent/runtime/supervisor.py", line 141, in _wrap_call_task
    await run_call_task(...)
  File ".../voiceagent/runtime/call_task.py", line 577, in run_call_task
    await _run_pumps_with_remote_hangup_detection(...)
  File ".../voiceagent/runtime/call_task.py", line 443, in _run_pumps_with_remote_hangup_detection
    await pumps_task
  File ".../voiceagent/runtime/call_task.py", line 380, in _run_pumps
    await asyncio.gather(caller_task, events_task)
  File ".../voiceagent/runtime/call_task.py", line 343, in pump_engine_events
    await media_stream.send(event.frame)
  File ".../voiceagent/telephony/freeswitch/media.py", line 211, in send
    await self._flush(chunk)
  File ".../voiceagent/telephony/freeswitch/media.py", line 191, in _flush
    await self._socket.send_text(json.dumps(envelope))
  File ".../voiceagent/telephony/freeswitch/media_transport.py", line 148, in send_text
    await self._send(text)
  ...
websockets.exceptions.ConnectionClosedOK: received 1000 (OK) Normal closure; then sent 1000 (OK) Normal closure
```

**Reproduced deterministically, hermetically, without real infra** --
`tests/integration/test_runtime_integration.py::
test_media_transport_dying_mid_turn_reaches_a_terminal_status`, new this
phase, `@pytest.mark.xfail(strict=True)`: asserts the *correct* desired
behavior (`final.status in {"failed", "interrupted"}`), which currently
fails (reports `XFAIL`) because the real, current behavior is
`status == "completed"`. Run 5 times in a row during this phase, `XFAIL`
every time -- fully deterministic, no flakiness. `strict=True` means that
if the underlying defect is ever fixed, this test flips to an unexpected
pass and fails the suite, forcing the marker's removal rather than the
test silently going stale.

**What this phase did NOT do, per its own brief section 8**: did not touch
`voiceagent/`, did not attempt a fix, did not broaden scope. This is
reported here for review; a fix (e.g., wrapping the `await
_run_pumps_with_remote_hangup_detection(...)` call at
`call_task.py:577` in an `except Exception` that sets
`cancellation.reason = "media_disconnect"` before re-raising, or an
equivalent narrow change) is a decision for a future, explicitly-scoped
phase.

**The original FAILED finding above is preserved verbatim and is not
retracted or edited.** It documents the defect as found. Section 1a below
documents the remediation applied in a separate, later work session, after
review and explicit approval of the fix described there.

## 1a. Remediation applied (later session, reviewed and approved)

**Root cause** (confirmed by reading, not guessed): two independent gaps,
both required to fix together:

1. `voiceagent/runtime/call_task.py` -- `CancellationSignal.reason` stayed
   at its `"hangup"` default because nothing on the "an ordinary exception
   escaped the pump" path ever set it to `"media_disconnect"`, even though
   `_final_status()` already had correct, dedicated (dead) handling for
   that value.
2. `voiceagent/telephony/freeswitch/media_transport.py` --
   `WebSocketMediaSocket.send_text()` propagated the real `websockets`
   library's own `ConnectionClosed` exception unchanged, contradicting
   `voiceagent/telephony/contracts.py`'s own explicit, documented
   invariant: "An adapter never lets a transport-specific exception
   escape: the runtime reacts to this taxonomy, not to a vendor's
   exception types." Fixing only (1) with a bare `except Exception` would
   have violated the brief's own explicit prohibition ("Do not catch
   `Exception` broadly and silently convert every exception into media
   failure") and risked misclassifying unrelated engine/tool failures.
   Fixing (2) first restores the module's own documented contract, which
   makes a narrow `except TransportError` in (1) both correct and
   sufficient.

**Exact fix, three files, narrowly scoped:**

* `voiceagent/telephony/freeswitch/media_transport.py` -- `serve_freeswitch_media()`
  gained a `_send()` wrapper symmetric to the pre-existing `_recv()` one,
  converting a real `websockets.exceptions.ConnectionClosed` on send into
  the module's own private `_ConnectionClosed` signal (previously only the
  receive path did this). `WebSocketMediaSocket.send_text()` now catches
  `_ConnectionClosed` and raises `TransportError` -- the same taxonomy
  every other telephony/media failure in this codebase already uses, never
  a new status category.
* `voiceagent/telephony/freeswitch/media.py` -- no behavior change; one
  stale comment (in `_FreeSwitchMediaStream.close()`) describing the old
  "propagates unchanged" behavior corrected to describe the new
  `TransportError` behavior, since `contextlib.suppress(Exception)` there
  already covers `TransportError` (a subclass of `Exception`) exactly as
  it covered the old raw exception -- that call site needed no change.
* `voiceagent/runtime/call_task.py` -- the `await
  _run_pumps_with_remote_hangup_detection(...)` call is now wrapped in
  `except TransportError: cancellation.reason = "media_disconnect"; raise`.
  Narrowly scoped to `TransportError` alone, never a bare `except
  Exception`: `TransportError` is raised only by a telephony/media adapter
  (confirmed by grepping every real usage of `voiceagent.telephony
  .contracts.TransportError` in this codebase -- exclusively
  `voiceagent/telephony/*` and `voiceagent/runtime/orchestrator.py`'s own
  FreeSWITCH-specific handling; a same-named but unrelated `httpx
  .TransportError` is what the LLM/TTS provider adapters use, confirmed
  distinct by reading their imports). The original exception is always
  re-raised, never swallowed -- the existing `call.task.failed` supervisor
  error log is unchanged; only the finalization reason changes.

**Explicitly preserved, unchanged:** normal caller hangup (`"hangup"` ->
`completed`), explicit outer cancellation, normal provider hangup,
successful completion, cross-call/cross-tenant isolation, and every
Phase 2.29 NOT VALIDATED/PARTIAL finding from sections 2, 5, and 6 below
(none of those limitations were about this defect and none are resolved by
this fix -- see section 5 for the explicit list, still current).

**Regression coverage** -- `tests/integration/test_runtime_integration.py`:

* `test_media_transport_dying_mid_turn_reaches_a_terminal_status` -- the
  Phase 2.29 `xfail(strict=True)` reproduction, converted to a real,
  unmarked, passing test. No longer monkeypatches a fake stream to raise a
  generic `ConnectionError`; raises `TransportError` instead (matching what
  the real, fixed transport now raises), and asserts the precise
  post-fix outcome: `status == "failed"`, `end_reason == "media_disconnect"`
  (previously asserted only `status in {"failed", "interrupted"}` as an
  xfail target). Run 5 times in a row this session: passed every time.
* `test_an_unrelated_pump_exception_is_not_misclassified_as_media_disconnect`
  -- new. A plain `RuntimeError` raised from the engine's own `events()`
  stream (not from `media_stream.send()`) must not produce
  `end_reason == "media_disconnect"` -- proves the classification is keyed
  on exception *type* from the *media* boundary specifically, not "some
  exception happened during the pump."
* `test_run_call_task_happy_path_completes_and_finalizes` (pre-existing,
  unmodified) already covers normal hangup -> `completed`/`completed` and
  is unaffected by this fix (it never raises `TransportError`) -- rerun as
  part of the full suite below, still green.

**Before/after, hermetic:**

| | Before this fix | After this fix |
| --- | --- | --- |
| `test_media_transport_dying_mid_turn_...` | `XFAIL` (asserted `status == "completed"`, the bug) | `PASSED` (`status == "failed"`, `end_reason == "media_disconnect"`) |
| Unrelated-exception test | did not exist | `PASSED` (`end_reason != "media_disconnect"`) |
| Normal hangup / successful-call tests | `PASSED` | `PASSED`, unchanged |

**Real staging re-verification, after the fix, `p224-freeswitch`, real
Deepgram/OpenAI credentials** (same `--stop-media-call-index N` mechanism
as the original section 2 finding, same script, no new production seam):

| Trial | Calls | Induced-failure call | Result | Other calls | Cross-contamination |
| --- | --- | --- | --- | --- | --- |
| 1 | 2 | call 1: media stopped via real `api uuid_audio_stream <uuid> stop` | `status='failed'`, `end_reason='media_disconnect'` -- **correct, was `completed` before the fix** | call 2: `status='completed'`, own reply verified, PASS | none (2 distinct tenants/CallSessions/fs_channel_uuids of 2) |
| 2 | 3 | call 2: media stopped identically | `status='failed'`, `end_reason='media_disconnect'` -- correct | calls 1, 3: `status='completed'`, own reply verified, PASS | none (3 distinct tenants/CallSessions/fs_channel_uuids of 3) |

Both trials' own real tracebacks are identical in shape to the original
finding's (`websockets.exceptions.ConnectionClosedOK` -> now caught inside
`media_transport.py` and re-raised as `voiceagent.telephony.contracts
.TransportError` -- visible in this session's own captured output, not
paraphrased) -- the exception is still genuinely raised and still reaches
`run_call_task()`; only its type and the resulting `CancellationSignal
.reason`/final status differ.

**Section 2's original checklist item "affected CallSession reaches an
appropriate terminal state" is hereby re-graded from FAILED to GREEN,
confirmed by the two real trials directly above.** Every other checklist
item in section 2 was already GREEN before this fix and is unaffected by
it.

**Validation gates after the fix** (full detail in section 10, unchanged
structure): hermetic `pytest -q` all passed (no `xfail` remaining);
`pytest tests/integration/ -m integration -q` all passed, exit 0; Ruff
check clean; Ruff format clean; Pyright 0/0/0; import-linter 8/0;
detect-secrets 0 findings (baseline mutation reverted, never staged);
pip-audit clean; `git diff --check` clean.

## 2. Real per-call media disconnect (brief section 2)

**Category: REAL STAGING. Result: FAILED** (the specific "appropriate
terminal state" claim; every other sub-claim below is GREEN).

**Mechanism used**: `FreeSwitchTelephonyProvider.stop_media_stream()`
(`voiceagent/telephony/freeswitch/provider.py:333-334`) -- a real,
pre-existing, production ESL command
(`api uuid_audio_stream <call_ref> stop`) that tears down exactly one
call's own FreeSWITCH `mod_audio_stream` session (its own inbound `wss://`
connection to this product's shared media listener) while that call's SIP
dialog stays up and every other concurrent call's own channel and media
socket are completely untouched. This is not a test-harness invention: it
is the same command `start_media_stream()`'s own docstring documents as
this product's existing FreeSWITCH-specific media-lifecycle seam. No
production code was changed to enable this experiment --
`scripts/validate_staging_concurrent_media_e2e.py` was extended with a new
`--stop-media-call-index N` flag that calls this existing method against
one selected call, mid-call, right after that call's caller audio has
streamed.

Two real trials, `p224-freeswitch`, real Deepgram/OpenAI credentials:

| Trial | Calls | Induced-failure call | Other calls | Cross-contamination |
| --- | --- | --- | --- | --- |
| 1 | 2 | call 1 (`fs_uuid=...8dcd-...`): media stopped, `send()` raised `ConnectionClosedOK`, `status='completed'` (wrong -- section 1) | call 2: `status='completed'`, own reply verified, PASS | none |
| 2 | 4 | call 2 (`fs_uuid=...226e-...`): identical pattern, `status='completed'` (wrong) | calls 1, 4: PASS, own reply verified; call 3: PARTIAL (own reply not verified intelligible -- see note below, unrelated to media) | none |

Checklist against the brief's own section 2 requirements:

* **affected call detects media loss**: GREEN -- `send()` raised a real
  exception the instant the transport was gone; this is genuine detection,
  not a timeout/guess.
* **affected CallSession reaches an appropriate terminal state**:
  **FAILED** -- it reaches *a* terminal state (`completed`), but the wrong
  one (see section 1). This is the specific, evidence-backed defect.
* **no orphaned media task remains**: GREEN -- `run_call_task()`'s
  `finally` block runs unconditionally regardless of which exception path
  is taken (confirmed by reading `call_task.py:587-627`); `media.detach()`
  is still called, bounded by `media_detach_timeout_seconds`.
* **no orphaned AI task remains**: GREEN -- same `finally` block calls
  `engine_session.close()`, bounded by `engine_close_timeout_seconds`,
  before `finalized` is checked.
* **no unexpected executor/task remains**: GREEN -- both real trials'
  other concurrent call(s) completed normally on the identical shared
  `DatabaseBoundary`/`CallRuntime`/ESL connection/media listener objects,
  with no observed hang, timeout, or resource error in either trial.
* **FreeSWITCH channel is cleaned up appropriately**: GREEN -- the induced
  call's SIP dialog was still cleanly terminated by the script's own
  scheduled `BYE` in both trials (the media-stop command does not itself
  hang up the channel, by design -- that is what makes it a genuine
  per-call *media* disconnect rather than a call teardown).
* **unrelated concurrent call continues**: GREEN, both trials -- verified
  via distinct real `CallSession`, tenant, and `fs_channel_uuid` for every
  call, own-reply-verified for the non-induced calls.
* **unrelated call retains correct tenant/session/call identity**: GREEN,
  both trials -- see the `[cross-call]` isolation line each script run
  prints (4 distinct tenants/sessions/fs_channel_uuids of 4 calls in
  trial 2).
* **no audio/transcript leakage occurs between calls**: GREEN, both
  trials -- zero cross-contamination detected (no call ever heard another
  call's own NATO keyword).

**Note on call 3's PARTIAL result in trial 2**: its own real LLM response
was `"I'm unable to provide that information."` instead of the scripted
code-word reply -- an unrelated real-model behavior (the CHARLIE ONE
THREE TANGO test fixture's own agent prompt/phrase apparently reads, to
the real LLM, as an information request it should refuse), not a media,
concurrency, or cancellation defect. Out of this phase's scope; not
investigated further, consistent with the brief's own instruction not to
reopen adjacent findings.

## 3. Real cancellation during active AI/media work (brief section 3)

**Category: REAL STAGING + HERMETIC-MOCKED. Result: GREEN.**

Following Phase 2.28's own honest pattern (its section 7): real elapsed
wall-clock time cannot pin an exact pipeline stage in a live network round
trip, so real trials establish "hung up at approximately time T, reached a
clean terminal state" while the Phase 2.28 hermetic parametrized test
(`test_hangup_mid_stalled_ai_turn_leaves_a_concurrent_call_unaffected`,
`stt`/`llm`/`tts`, still green, unchanged this phase) remains the
authoritative stage-precise proof.

Two real trials via `--early-hangup-seconds` (unchanged from Phase 2.28,
reused as-is):

| `early_hangup_seconds` | Target window | Result |
| --- | --- | --- |
| 0.3s | immediately after caller speech, before AI reply audible | real BYE reached `status='completed'` cleanly, no crash |
| 3.5s | mid-reply-playback window | real BYE reached `status='completed'` cleanly, own reply verified before hangup |

Both are genuine, real caller-hangup (`CallEventType.HUNGUP`) paths --
`_watch_for_remote_hangup()` correctly sets `cancellation.reason =
"hangup"` and `_final_status("hangup")` correctly returns
`("completed", "completed")` in both cases (this is the *correct*
behavior for an actual hangup -- a useful, direct contrast against
section 1's finding, which is specifically about the *absence* of any
such signal for a non-hangup transport failure).

This phase's own new hermetic test (section 1) additionally exercises
cancellation-adjacent behavior for the *transport-death* case
specifically, which the existing Phase 2.28 `stt`/`llm`/`tts` parametrized
test does not cover (that test wedges the *engine*, not the *transport*).

## 4. Concurrent failure isolation (brief section 4)

**Category: REAL STAGING. Result: GREEN (isolation properties); FAILED
(terminal-status correctness, same root cause as section 1) --
subsequently FIXED, re-graded GREEN, and re-verified live at 2- and 3-call
concurrency (section 1a's own post-fix re-verification table is itself a
Scenario A/B rerun: same induced-failure-plus-surviving-calls shape, same
isolation properties reconfirmed, now with the correct terminal status
too).**

### Scenario A (2 calls)

Reuses section 2's trial 1 directly (call 1: induced media failure; call
2: normal spoken interaction). Call 2 recorded:

* tenant: distinct from call 1's own tenant (fresh per-call provisioning)
* `CallSession`: `distinct`
* `fs_channel_uuid`: distinct
* SIP Call-ID: distinct
* final status: `completed`, own reply (`"bravo"`) verified, no
  contamination

### Scenario B (4 calls)

Reuses section 2's trial 2 directly (call 2: induced media failure; calls
1, 3, 4: continue). Full per-call record:

| Call | Tenant | CallSession | fs_channel_uuid | SIP Call-ID | Final status | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `e40cd65d-...` | `345539ff-...` | `...2175...` | `c844mid4...` | completed | PASS, own reply verified |
| 2 (induced) | `56449c8e-...` | `c46d1627-...` | `...226e...` | `m8mc9x2h...` | completed (wrong -- section 1) | media disconnected as designed |
| 3 | `0bafb9c4-...` | `d5a9501e-...` | `...215e...` | `7m15r9m7...` | completed | PARTIAL, unrelated LLM-refusal (section 2 note) |
| 4 | `866e9f95-...` | `dc53c855-...` | `...218d...` | `l5ymcgpt...` | completed | PASS, own reply verified |

Zero cross-call or cross-tenant leakage in either scenario: 4 distinct
tenants, 4 distinct `CallSession`s, 4 distinct `fs_channel_uuid`s, of 4
calls, every trial. No call ever heard another call's own NATO keyword.

## 5. Long-running stability / soak (brief section 5)

**Category: REAL STAGING. Result: PARTIAL** -- genuinely bounded by real
provider API cost/time, exactly as the brief itself anticipates and as
Phase 2.27/2.28 were also bounded. **This phase does not, and explicitly
does not claim to, rule out a slow memory leak from this sample size** --
per the brief's own explicit instruction ("Do not declare absence of
memory leaks from a short sample").

14 real calls total across this phase (listed in full in section 8 below),
spanning: 6 sequential single calls, one 2-call concurrent run, one
4-call concurrent run, one additional 2-call concurrent run -- i.e.
repeated sequential + repeated 2-call + one 4-call trial, matching the
brief's own preferred shape. 3-call concurrency specifically was not
separately re-run this phase (already covered at 2/6/8/6 real trials
across Phase 2.27 section 7 and Phase 2.28 section 4); reusing that
existing real evidence rather than re-spending real provider budget on an
identical shape was a deliberate choice, consistent with the brief's own
"the objective is not maximum load testing."

**Measured**:

* successful/terminal calls: 14 of 14 real calls reached a real terminal
  `CallSession` status (`completed` in every case -- including the two
  section-1/2/4 induced-failure calls, whose *status value* is itself the
  section 1 defect, not a validation-harness failure to reach *a*
  terminal state).
* real FreeSWITCH (`p224-freeswitch`) CPU, sampled via `docker stats`
  across every `--fs-container`-enabled run this phase: min 1.4%, avg
  ~4.4-9.7%, max 22.7% (4-call trial) -- consistent with, and not worse
  than, Phase 2.27's own measured range (1.7-25.9% across its own 1-4-call
  trials). No upward trend observed across this phase's own sequence of
  runs.
* this validation process's own CPU/wall time: consistent per-call
  (~2-3s CPU per call, ~10-27s wall depending on concurrency and
  early-hangup/media-stop timing) -- no growth trend across the sequence
  of runs, but each run is a **separate OS process** (see the explicit
  limitation immediately below).
* STT/LLM/TTS completion: all 14 calls produced a real, persisted
  assistant turn (even the two induced-media-failure calls -- the LLM
  response was generated and persisted before the outbound `send()`
  failed, confirming the STT->LLM path is itself unaffected by an
  *outbound* media failure).
* audio bytes/duration, task counts, WebSocket/session counts,
  Python-process memory, DB-connection/executor internal state: **NOT
  VALIDATED as a growth-over-time series** -- this harness (inherited from
  Phase 2.27/2.28, unchanged in this respect) launches one fresh OS
  process per script invocation, so there is no single long-lived Python
  process whose own RSS/task-count/connection-pool state could be sampled
  across all 14 calls to look for monotonic growth. The four calls that
  *did* share one live process (the 4-call concurrent trial's own
  `asyncio.gather` batch) showed no observable resource anomaly, but four
  calls in one process is far too small a sample to say anything
  meaningful about a slow leak.

**Honest limitation, stated plainly**: this phase's own real-staging
evidence supports "no acute resource blowup across 14 real calls, several
concurrency levels, several induced failures" -- it does **not** support
"no slow memory/task/connection leak over hundreds or thousands of calls
on one long-lived process," which would require either a genuinely
long-lived harness process (a real architectural change to the validation
tooling, out of this phase's scope) or a production deployment's own
long-run observability (outside this repo entirely).

## 6. Repeated failure cleanup (brief section 6)

**Category: REAL STAGING. Result: PARTIAL** -- real, but scoped narrower
than a literal reading of the brief, with the scoping made explicit here
rather than silently assumed.

What was actually run: after each of this phase's two induced-media-stop
trials (section 2), a further real call was run:

* Trial 1 (2-call, call 1 induced): call 2, in the **same**
  `asyncio.gather` batch, on the **identical** live `DatabaseBoundary`/
  `CallRuntime`/ESL connection/media listener objects as call 1, started
  and completed normally *after* call 1's own failure had already
  propagated and been finalized (call 1's `send()` exception fires within
  ~1-2s of the induced stop, well before call 2's own ~14s completion) --
  this is real, same-object-graph, post-failure recovery evidence on the
  shared runtime, satisfying the brief's own core concern ("cleanup did
  not poison the shared runtime") directly.
* A separate, fresh script invocation (`--calls 1`, no induced failure)
  was run immediately after both induced-failure trials completed: PASS,
  clean, own reply verified -- real evidence that the real FreeSWITCH
  container/dialplan/phone-number-registration layer was not left in a
  bad state by two prior real per-call media-stream stops.

**Explicit scoping limitation**: this second check is **process-level**,
not **object-level** -- this harness's own `_run_shared()` tears down and
rebuilds its `ManagedEslConnection`/`FreeSwitchMediaProvider`/
`CallRuntime`/`DatabaseBoundary`/`CallOrchestrator` on every separate
script invocation, so it does not by itself prove the *identical, still
running* `CallRuntime`/`DatabaseBoundary` Python objects tolerate an
arbitrary number of sequential failures without accumulating state. The
*first* check above (same-batch, same-object-graph) is the stronger of
the two and is real, but only demonstrates concurrent (not strictly
sequential) co-existence of one failure and one success on the same
objects, not a long chain of sequential failures on one live runtime. A
harness change to support many sequential rounds against one persistent
set of shared objects (a `--rounds N` flag looping the provision+run
phase while keeping the same ESL/media/runtime/db objects alive across
rounds) would close this gap; it was not built this phase, to avoid a
larger, riskier harness change late in an already large validation pass --
flagged here as a concrete, scoped follow-up rather than silently
substituted for.

**Different-tenant check**: satisfied directly and repeatedly -- every
multi-call trial this phase (and every prior phase's own harness
convention, `_provision_fixture()`) provisions a **fresh tenant per call**,
so every non-failing call in the same batch as an induced failure (calls
2/1/3/4 across this phase's two induced-failure trials) is itself real
evidence of "a different tenant can make a call successfully on the same
shared runtime immediately after another tenant's call failed."

## 7. Security validation (brief section 7)

**Category: REAL STAGING (identity capture) + code review. Result:
GREEN.** No authorization code was read for modification, let alone
changed, this phase.

* **media ticket remains tenant-bound / call-session-bound**: unchanged --
  this phase never touches `media_transport.py`'s ticket
  minting/verification (`voiceagent/telephony/freeswitch/
  media_transport.py:66-117`, untouched). `stop_media_stream()` (section
  2's own mechanism) takes only a `call_ref` (the FreeSWITCH channel UUID)
  already captured from that call's own real `CallSession` row -- it
  cannot be pointed at another call's channel without already knowing that
  other call's own `fs_channel_uuid`, itself never exposed cross-tenant by
  anything this phase touched.
* **failed call cannot affect another tenant**: GREEN, directly
  demonstrated -- every non-induced call in every multi-call trial this
  phase ran belonged to a different, freshly-provisioned tenant than the
  induced-failure call, and completed normally in every trial (section 4).
* **media events cannot cause cross-call cleanup**: GREEN by code review
  -- `FreeSwitchMediaProvider.detach()`
  (`voiceagent/telephony/freeswitch/media.py:295-307`) is keyed
  exclusively by `call_ref` in a per-call `dict`; nothing in this phase's
  own reading of `media.py`/`call_task.py` found any code path where one
  call's own media event/failure could reach another call's own
  `_streams`/`_sockets` entry. Confirmed empirically too: the induced
  call's own failure never affected any concurrent call's own audio/
  transcript in any trial (zero cross-contamination, sections 2/4).
* **failure handling cannot bypass authorization**: GREEN by code review
  -- `run_call_task()`'s own `authorize_call_data_access()` gate
  (`call_task.py:509-519`) runs once, before the pump loop even starts;
  section 1's defect is purely in *post-hoc status reporting*, entirely
  downstream of and unrelated to the authorization gate, which this phase
  confirmed is unchanged and unreachable-a-second-time from any failure
  path read this phase.
* **provider callbacks remain correlated to the correct call**: GREEN --
  every real trial's own per-call `fs_channel_uuid`/`CallSession`/SIP
  Call-ID triple was distinct and consistently matched throughout
  (sections 2, 4).
* **no sensitive credentials enter logs or generated artifacts**: GREEN --
  this document, this phase's own script changes, and this phase's own
  test additions were reviewed before being written; no credential, ticket
  secret, or raw audio appears anywhere in this phase's own new code or
  this document (the `media_ticket_secret` used by the shared-infra
  harness is the same non-secret placeholder string Phase 2.27/2.28 already
  used and already reviewed, `"phase227-validation-secret"`, unchanged).

No authorization boundary was touched, let alone weakened, this phase.

## 8. Real-staging call inventory (14 calls total, this phase)

| # | Purpose (brief section) | Calls | Real FreeSWITCH container | Notes |
| --- | --- | --- | --- | --- |
| 1 | post-edit sanity check | 1 | p224-freeswitch | PASS |
| 2 | section 2/4A: induced media-stop, 2-call | 2 | p224-freeswitch | call 1 induced; call 2 PASS |
| 3 | section 2/4B: induced media-stop, 4-call | 4 | p224-freeswitch | call 2 induced; calls 1,4 PASS, call 3 PARTIAL (unrelated) |
| 4 | section 6: post-failure fresh call | 1 | p224-freeswitch | PASS |
| 5 | section 3: early-hangup 0.3s | 1 | p224-freeswitch | PASS (clean terminal state) |
| 6 | section 3: early-hangup 3.5s | 1 | p224-freeswitch | PASS (clean terminal state) |
| 7 | section 5: soak, sequential #1 | 1 | p224-freeswitch | PASS |
| 8 | section 5: soak, sequential #2 | 1 | p224-freeswitch | PARTIAL (known "alpha"->"also" STT variability, Phase 2.28) |
| 9 | section 5: soak, 2-call #2 | 2 | p224-freeswitch | 1 PASS, 1 PARTIAL (same known STT variability) |

Total: 14 real calls, real Deepgram STT/Aura TTS, real OpenAI LLM, real SIP
INVITE/BYE, real RTP, real ESL commands, zero simulated infrastructure.

## 9. Existing known findings -- not reopened

* **Phase 2.28's "alpha"->"also" STT finding**: recurred twice more this
  phase (run 8 and run 9's first call, section 8 table above), consistent
  with Phase 2.28's own characterization (provider/word-specific
  recognition variability, reproduces at any concurrency level, no media
  corruption, no cross-session leakage). Not reinvestigated, per this
  phase's own brief section 9.
* **`test_one_call_failing_does_not_affect_others`**: not modified. Not
  specifically re-run in isolation this phase (Phase 2.28 already
  characterized it as a pre-existing fixed-sleep race); it passed as part
  of every full integration-suite run this phase performed (no isolated
  re-characterization attempted, per the brief's own instruction not to
  "fix" it).
* **`test_run_call_task_happy_path_completes_and_finalizes`**: same
  treatment -- unmodified, passed as part of every full-suite run this
  phase, not re-characterized in isolation.

## 10. Validation gates

* `pytest -q` (hermetic default suite): **all passed**, zero failures
  (this phase's own new test lives under `tests/integration/`, excluded
  from this run by design, same as every existing integration test).
* `pytest tests/integration/ -m integration -q` (real PostgreSQL,
  `saas_os_app`/`saas_os` roles against the existing `voiceagent-test-pg`
  container, port 15432): **exit code 0, all passed**, with exactly one
  `x` (`XFAIL`, this phase's own new
  `test_media_transport_dying_mid_turn_reaches_a_terminal_status` --
  expected, documents the section 1 defect, `strict=True`). No `F`
  (failure) or `E` (error) markers in the run.
* Targeted Phase 2.29 test
  (`test_media_transport_dying_mid_turn_reaches_a_terminal_status`): run
  5 times in isolation, **`XFAIL` all 5**, fully deterministic.
* Ruff check: **1 real finding caught and fixed this phase** (`S101 Use
  of assert detected`, `scripts/validate_staging_concurrent_media_e2e.py`
  -- a bare `assert telephony is not None` used for an internal-invariant
  narrowing; replaced with an explicit `if telephony is None: raise
  ValueError(...)`). **All checks passed** after the fix.
* Ruff format --check: **354 files already formatted**.
* Pyright: **0 errors, 0 warnings, 0 informations**.
* import-linter: **8 contracts kept, 0 broken**.
* detect-secrets (`.secrets.baseline`): **0 real findings** -- the
  scan's own known in-place `generated_at`/reordering mutation occurred
  again this phase (as it does on every invocation, Phase 2.27/2.28's own
  documented gotcha), diffed to confirm it was only that, then reverted
  with `git checkout -- .secrets.baseline` before ever being staged.
* pip-audit: **no known vulnerabilities** (`saas-os`/`voiceagent` skipped,
  not on PyPI, expected and unchanged from every prior phase).
* `git diff --check`: **clean**.

## 11. Files changed

```text
scripts/validate_staging_concurrent_media_e2e.py (extended)  -- --stop-media-call-index
                                                                  (brief section 2: a real
                                                                  per-call media disconnect
                                                                  via the existing
                                                                  stop_media_stream() ESL
                                                                  command), CallTimeline
                                                                  .media_stop_at
tests/integration/test_runtime_integration.py (extended)     -- one new hermetic,
                                                                  deterministic,
                                                                  xfail(strict=True)
                                                                  reproduction of the
                                                                  section 1 defect
docs/PHASE-2.29-REAL-MEDIA-FAILURE-STABILITY.md (new)         -- this document
```

**No file under `voiceagent/` was changed.** No production code changed
this phase, per its own default expectation (brief: "validation-only")
and its own section 8 procedure for a discovered defect (reproduce,
document, report for review -- do not fix unilaterally).

## 12. SaaS-OS pin

Unchanged and unmodified: `ff550010e5eafecace7311038aadc99fcecfbe3d`.

## 13. Final status

Per this phase's own instructions, no commit has been created and nothing
has been pushed. `docs/PHASE-0-ARCHITECTURE.md` and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain exactly as
they were at the start of this phase -- neither staged, modified, nor
committed.
