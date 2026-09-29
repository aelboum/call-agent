# Phase 2.30: Long-Running Stability & Repeated-Failure Cleanup Validation

Baseline: `a5fc790` (post Phase 2.29 remediation, `origin/main`). SaaS-OS
remains pinned and unmodified at `ff550010e5eafecace7311038aadc99fcecfbe3d`.
`docs/PHASE-0-ARCHITECTURE.md` and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched. Per
this phase's own instructions, no commit exists for this phase's own work
and none has been pushed.

This phase is primarily validation/observability. No production
`voiceagent/` file was changed in the original Phase 2.30 session. One
genuine, reproducible production defect was found during resource-leak
investigation (section 1) and was reported for review -- **not fixed** in
the original session, per that session's own instructions.

**UPDATE (later, separate, reviewed and approved session): fixed.** See
section 1a for the root cause, exact remediation, regression coverage, and
real-staging re-verification. Everything below this paragraph, through the
end of section 1, reflects the ORIGINAL Phase 2.30 finding, preserved as
found; section 1a documents the fix and the re-graded, now-GREEN result on
top of it.

**Status vocabulary used throughout**: category is one of **REAL STAGING**
/ **HERMETIC** / **NOT VALIDATED** (never blurred); result is one of
**GREEN** (genuinely validated) / **PARTIAL** (some but not all requested
evidence) / **NOT VALIDATED** (could not safely establish) / **FAILED**
(reproducible defect).

## 0. Headline result

```text
Section 1  (resource-leak investigation)        REAL STAGING, FAILED -- new defect found, reported;
                                                                       FIXED and re-graded GREEN, see section 1a
Section 3  (sequential long-running soak)       REAL STAGING, GREEN -- 24 sequential calls, one long-lived process
Section 4  (repeated successful calls, resources) REAL STAGING, PARTIAL -- stable-looking sample, honestly bounded
Section 5  (repeated failure -> recovery)       REAL STAGING + HERMETIC, GREEN (functional) / see section 1 for leak
Section 6  (concurrent failure recovery)        REAL STAGING, GREEN (after a test-harness artifact was diagnosed
                                                                       and ruled out -- see section 6.1)
Section 7  (recovery after multiple failures)   REAL STAGING + HERMETIC, GREEN
Section 9  (security / isolation)               REAL STAGING, GREEN
```

## 1. Production defect found -- reported for review, not fixed

**This is the headline finding of this phase, flagged first per this
phase's own brief section 11.**

**Symptom.** `voiceagent.runtime.supervisor.CallRuntime` keeps a private
`dict[uuid.UUID, BaseException]` called `_errors`, populated every time a
call's own task raises out of `run_call_task()`:

```python
# voiceagent/runtime/supervisor.py, _wrap_call_task()
except Exception as exc:  # noqa: BLE001
    self._errors[call_session_id] = exc
    ...
```

The only place anything is ever removed from `_errors` is `start_call()`,
which pops the *same* `call_session_id` before starting it:

```python
# voiceagent/runtime/supervisor.py, start_call()
self._errors.pop(call_session_id, None)
```

A real call's `call_session_id` is a fresh UUID, generated once when the
`CallSession` row is created, and is **never reused** for a different
call. `start_call()` is therefore never invoked again for the same id once
that call has finished, so an entry added to `_errors` on failure is never
removed for the remaining lifetime of the `CallRuntime` process. Every
failed call -- including every ordinary, correctly-classified media
disconnect (`TransportError` -> `"media_disconnect"`, Phase 2.29's own fix,
still correct and unmodified by this phase) -- permanently leaks one
`BaseException` object (with its full traceback, and everything that
traceback's frames close over) into this dict for as long as the process
runs.

**This is not a hypothetical read of the code.** It was directly observed
during this phase's own real-staging sequential soak (section 3): each of
the sequence's four induced media-disconnect failures increased
`len(CallRuntime._errors)` by exactly one, and the count never decreased,
including across the many normal, successful calls that ran afterward:

| checkpoint (after N calls) | `len(CallRuntime._errors)` | cumulative media_fail calls so far |
| --- | --- | --- |
| 0 | 0 | 0 |
| 5 | 0 | 0 |
| 10 | 1 | 1 (call 6) |
| 20 | 3 | 3 (calls 6, 11, 16) |
| 24 | 4 | 4 (calls 6, 11, 16, 21) |

The count tracks the number of failed calls exactly, with no eviction, no
cap, and no TTL. In a real production `call-runtime` process handling
failures over days or weeks, this dict grows without bound.

**Root cause.** `CallRuntime._errors` was designed as a per-call scratch
slot keyed by `call_session_id`, cleared on that same id's *next* start
(`start_call()`'s own pop). That design implicitly assumes a bounded,
reused key space (plausible if `call_session_id` were e.g. a channel slot
number) -- but the actual key is a globally unique, never-reused UUID
minted once per real call. The cleanup path that exists (`pop` on restart)
can therefore structurally never fire for real traffic.

**Severity assessment (not a fix, an honest read).** The leaked value is
one `BaseException` per failed call -- typically small, but holding a full
traceback (stack frames, and anything they close over, e.g. large request
payloads if a failure ever occurs holding one) means the leak's *rate* is
bounded by call volume but its *per-entry size* is not bounded by this
code path at all. This phase's own 24-call soak (4 failures) shows no
externally-visible RSS effect yet (section 4) -- the process-level growth
from 4 small `RuntimeError`/`TransportError` objects is far below this
soak's own measurement noise floor. This is exactly why the brief
distinguishes "detection of monotonic resource growth" from "proof of
absence of leaks": a 24-call, single-session soak cannot rule out that
this dict becomes materially large only after thousands of failures over a
long production lifetime, and cannot rule out that some failure modes
(e.g. an authorization or tool-gateway exception carrying a larger closure)
leak more per entry than the ones exercised here.

**Not fixed.** Per this phase's own brief section 11: reproduced, root
cause determined, documented here, not silently fixed, flagged for review
before any scope expansion. A hermetic regression test was added
(`tests/integration/test_runtime_integration.py
::test_repeated_failures_do_not_poison_later_calls`, section 5) that
proves the **functional** property the brief actually asked this phase to
validate -- repeated failures do not block later calls from succeeding --
without asserting on `_errors`'s own long-run size, so as not to encode
this bug's current behavior as an intended contract in a way that would
need to be un-asserted the moment it is fixed.

## 1a. Remediation (later, separate, reviewed and approved session)

**Root cause, restated precisely.** `CallRuntime._errors`
(`voiceagent/runtime/supervisor.py`) is written on every call-task failure
(`_wrap_call_task()`'s `except` clause) and was, before this fix, removed
only by `start_call()` popping the *same* `call_session_id` on a restart --
a path that structurally never fires for real traffic, because a real
call's `call_session_id` is a fresh UUID, never reused. `_tasks` and
`_cancellations` are correctly popped in `_wrap_call_task()`'s own
`finally`; `_errors` was the one exception to that symmetry, with no
cleanup path connected to real call volume at all.

**Design constraint that shaped the fix.** `error_for()` is read by every
existing caller (this codebase's own tests, and the only present callers
of it) *after* the failing call is already confirmed not running --
typically via `_wait_until(lambda: runtime.error_for(cid) is not None)` or
an equivalent poll. `_wrap_call_task()`'s `except` and `finally` clauses
run back-to-back with no `await` between them, so from any other
coroutine's point of view, "task no longer running" and "task's own
`finally` has completed" are the same instant. This means `_errors` cannot
be popped inside that same `finally` block (alongside `_tasks` and
`_cancellations`) without making the entry unobservable to *any* external
poller, including every existing test -- there is no `await` boundary at
which "error recorded, not yet cleaned up" could ever be observed
externally. A same-instant pop was therefore ruled out, not attempted.

**The fix.** `_errors` is now reaped in `start_call()`, the moment a
*different* call starts on this runtime (`_reap_terminal_errors()`,
`voiceagent/runtime/supervisor.py`): every entry belonging to a call this
runtime is no longer actively supervising is evicted, guarded by an
explicit `call_session_id not in self._tasks` check that documents (and
would catch, if ever violated by a future change) the invariant that an
active call's error state must never be removed by another call merely
starting. This ties cleanup to the same real, ongoing signal that grows
the dict in the first place -- `start_call()` calls, which is exactly the
call pattern the real `CallOrchestrator` already uses in production, with
no new production call site, no TTL, no periodic sweep task, and no
arbitrary size cap. The trade-off, stated plainly: a failed call's error
remains observable via `error_for()` for exactly as long as no *other* new
call has since started on this runtime -- unbounded only in the
pathological case of a runtime that stops receiving any new calls at all
after a failure, which is not the unbounded-forever-growth defect that was
found (a live call-runtime process that keeps taking traffic self-bounds;
one that goes permanently idle after a failure has no more calls to leak
memory over regardless).

**Why this trade-off is safe today.** A repository-wide search
(`grep -rn "error_for\|\._errors\b" voiceagent/`) confirms `error_for()`
has **zero production call sites** -- it is read only by this codebase's
own tests and by `scripts/validate_staging_long_running_soak.py`'s
read-only introspection. The durable record of a failed call is entirely
independent of `_errors`: the exception is logged
(`_logger.exception("call.task.failed", ...)`), a metric is recorded
(`record_runtime_call_startup_failure()`), and the terminal `CallSession`
row (`status="failed"`, `end_reason=...`) is persisted by
`run_call_task()`'s own `finally` in `voiceagent/runtime/call_task.py` --
a completely separate code path, untouched by this fix. Reaping `_errors`
removes only an in-process, same-runtime-lifetime diagnostic convenience,
never any durable business state.

**Files changed.**
* `voiceagent/runtime/supervisor.py` -- `_reap_terminal_errors()` added;
  called from `start_call()` in place of the old single-id
  `self._errors.pop(call_session_id, None)`; two docstrings updated
  (module-level "Error isolation" paragraph, `start_call()`'s own) to
  document the new lifecycle. No other behavior changed: `_tasks`/
  `_cancellations` cleanup, `cancel_call()`, `shutdown()`, and the
  `TransportError` -> `media_disconnect` classification in
  `voiceagent/runtime/call_task.py` (Phase 2.29, out of this fix's scope)
  are all untouched.
* `tests/runtime/test_supervisor.py` -- six new hermetic tests (below). No
  existing test in this file needed to change: both pre-existing tests
  that call `error_for()` after a failure
  (`test_one_calls_failure_is_isolated_from_another_calls_state` in this
  file, and the Phase 2.30-added
  `test_repeated_failures_do_not_poison_later_calls` in
  `tests/integration/test_runtime_integration.py`) only ever start
  exactly the calls under test, with no *third*, unrelated call starting
  in between -- so the reap, which only fires on a different call's
  `start_call()`, never fires within either test's own window, and both
  continue to pass unmodified.
* `docs/PHASE-2.30-LONG-RUNNING-STABILITY.md` -- this section.

**Regression tests added** (`tests/runtime/test_supervisor.py`, hermetic,
no database, no real FreeSWITCH):
1. `test_error_for_is_reaped_once_a_different_call_starts` -- a failed
   call's error is present immediately after failure, then gone the
   instant a different call starts.
2. `test_successful_call_leaves_no_error_state` -- a call that never fails
   never populates `_errors`.
3. `test_cancelled_call_leaves_no_error_state` -- a cancelled call (real
   `asyncio.CancelledError` path) never populates `_errors` either
   (unchanged pre-existing behavior, now with explicit coverage).
4. `test_repeated_failures_do_not_accumulate_in_the_errors_dict` --
   reproduces the exact defect shape hermetically: 10 sequential failing
   calls, asserting `len(runtime._errors)` immediately after each one is
   observed. Before this fix this would have printed `[1, 2, 3, ..., 10]`
   (the exact monotonic-growth shape measured in the real-staging soak,
   section 1); after the fix it is `[1] * 10` -- every failure is fully
   observable, and none accumulates past the next call.
5. `test_concurrent_calls_error_cleanup_is_isolated_and_bounded` -- two
   calls started concurrently (one fails immediately, one is held open and
   only later released to succeed) prove reaping never touches a
   still-running or already-resolved *other* call's state; a third,
   unrelated call starting afterward is what finally reaps the first
   call's entry, and touches nothing else.
6. `test_error_metric_and_log_are_recorded_independently_of_errors_dict_reaping`
   -- confirms `record_runtime_call_startup_failure()` fires for every
   failure regardless of when (or whether) `_errors` is later reaped,
   directly exercising the "error observability" requirement.

All six pass; the full hermetic suite is 958 passed (952 before this
phase's regression additions plus these 6), 261 deselected -- no other
test's behavior changed.

**Real-staging re-verification.** Two focused runs against the same real
topology as the rest of this phase (`p224-freeswitch`, real Postgres, real
Deepgram/OpenAI/Aura; no soak restart needed, `--calls`/`--sequence`
scoped small since a full 24-call soak was not needed to prove this fix):

* `--sequence normal,media_fail,normal,media_fail,normal` (5 real calls):
  `runtime_errors_dict_size_now` read `0, 1, 0, 1, 0` after each call in
  order -- the dict is reaped the instant the next call starts, never
  exceeding 1 entry, in contrast to the original soak's monotonic
  `0, 0, 1, 3, 4` over 24 calls. Both media-disconnect calls still reached
  `status='failed'`, `end_reason='media_disconnect'` (Phase 2.29's fix,
  confirmed unchanged). All 3 normal calls reached `status='completed'`
  with a real spoken round trip. Isolation: 5/5 distinct tenants,
  `CallSession` ids, FreeSWITCH UUIDs, and SIP Call-IDs -- no identity
  reuse.
* `--sequence normal,cancel,media_fail,normal` (4 real calls, added
  specifically to re-confirm cancellation behavior under this fix): the
  `cancel` call reached `status='completed'` normally (a real early caller
  hangup, `CallRuntime.cancel_call()`'s own path -- entirely untouched by
  this fix, and confirmed to never populate `_errors`, matching hermetic
  test 3 above); the `media_fail` call again reached
  `status='failed'`/`end_reason='media_disconnect'` with
  `runtime_errors_dict_size_now=1`, reaped back to `0` by the next call's
  start. 4/4 distinct tenants/sessions/FreeSWITCH-UUIDs/SIP-Call-IDs.

Both runs used only `CallRuntime.start_call()`/`cancel_call()` -- the same
calls the real `CallOrchestrator` makes in production -- with no
validation-only method added to or called on `CallRuntime`.

**Result: Section 1 re-graded REAL STAGING, GREEN.** The defect is fixed,
regression-covered hermetically, and re-verified against the real stack
with no change to media-disconnect classification, cancellation behavior,
or cross-call/cross-tenant isolation.

**Remaining limitation, stated honestly.** This fix bounds `_errors` by
call cadence, not by time or count in an absolute sense: a runtime that
fails a call and then never starts another one keeps that one entry
forever (it has nothing left to leak against, since it never starts
another call either). It does not add an explicit "give up waiting, evict
anyway" path, because that would be exactly the TTL/periodic-sweep
mechanism the brief for this remediation explicitly ruled out. This is a
deliberate, narrow, lifecycle-correct fix for the defect as measured and
described -- unbounded growth under continued real traffic -- not a claim
that no caller could ever construct a scenario where an entry outlives its
usefulness by an arbitrary amount; today, with zero production consumers
of `error_for()`, that residual case has no observable consequence.

## 2. Environment

* **Topology**: exactly ONE long-lived process/runtime throughout every
  real-staging scenario in this phase (sections 3-7, 9) -- one
  `ManagedEslConnection`, one `FreeSwitchMediaListener` (one port), one
  `FreeSwitchMediaProvider`, one `CallRuntime`, one `DatabaseBoundary`, one
  `CallOrchestrator`, matching Phase 2.27's own production-accurate
  topology. No process restart between any two calls within a given
  scenario run.
* **Real FreeSWITCH**: container `p224-freeswitch` (already running,
  healthy, pre-dating this phase by 2+ days), SIP on `127.0.0.1:15080`,
  ESL on `127.0.0.1:18023`. A second container, `p223-freeswitch`, exists
  in this environment but was deliberately not used, to keep topology
  consistent with the brief's "ONE long-lived runtime/process" requirement
  throughout.
* **Real PostgreSQL**: container `voiceagent-test-pg`, both SaaS-OS's own
  migrations and this product's already applied, connected as the
  restricted `saas_os_app` role (never a superuser/`BYPASSRLS` role) for
  application traffic, matching `tests/integration/README.md`'s own
  documented convention.
* **Real AI providers**: Deepgram (STT), Deepgram Aura (TTS), OpenAI
  `gpt-4o-mini` (LLM) -- the same real providers Phase 2.27/2.28/2.29 used,
  credentials from this environment's own pre-existing local staging
  credentials file (gitignored, never committed, never printed).
* **Network path**: this validation process runs on the Windows host, not
  inside a container; FreeSWITCH (in Docker Desktop's WSL2-backed VM)
  reaches back to it via `host.docker.internal`'s own resolved address
  (`192.168.65.254` in this environment). The real per-call media URL
  scheme must be `ws://`, not `http://` -- `mod_audio_stream` rejects a
  non-websocket URI outright (`invalid websocket uri`), discovered and
  corrected during this phase's own environment setup, before any call
  counted toward the results below.
* **Platform**: Windows 11, Python 3.13, this repository's own `.venv`.
  Resource sampling (`_rss_bytes()` in the new soak script) uses
  `ctypes`/`psapi.dll`'s `GetProcessMemoryInfo` directly -- no `psutil`
  dependency added, matching the brief's "no new production dependency"
  instruction (this is validation tooling, not production code, but the
  same discipline was applied).

## 3. Sequential long-running call validation -- REAL STAGING, GREEN

**New tool**: `scripts/validate_staging_long_running_soak.py`. Reuses
`scripts/validate_staging_concurrent_media_e2e.py`'s own fixture/call
helpers (`_provision_fixture`, `_run_call_on_shared_infra`, imported, not
copy-pasted) but drives them **one at a time**, sequentially, against
shared infra built once at process start -- the existing script's own
`_run_shared()` always launches its whole batch concurrently via
`asyncio.gather()`, which is the right shape for a concurrency question
(section 6) but the wrong shape for this section's own question (does the
same long-lived process survive many *successive* calls).

**Sequence run** (24 real calls, one long-lived process, zero restarts):

```text
normal normal normal normal normal  media_fail normal cancel normal normal
media_fail normal cancel normal normal  media_fail normal cancel normal normal
media_fail cancel normal normal
```

16 `normal`, 4 `media_fail` (a real per-call `api uuid_audio_stream <uuid>
stop` against only that call's own FreeSWITCH channel, Phase 2.29's own
mechanism), 4 `cancel` (a real early caller `BYE` 2 seconds after the
caller's own utterance ends, instead of waiting the full round trip -- the
same real-staging analog Phase 2.28 already established for "hangup during
active AI/media processing"; see section 5's own note on why genuine
`CallRuntime.cancel_call()`-initiated cancellation was not re-exercised
against the real stack here).

**Per-call results** (tenant/session/FreeSWITCH-UUID/SIP-Call-ID recorded
for every call; full detail in
`scripts/validate_staging_long_running_soak.py --json-out`'s own output,
not committed -- transient validation evidence, matching this repository's
convention of not committing script output):

| # | scenario | final status | end reason |
| - | - | - | - |
| 1-5 | normal | completed | completed |
| 6 | media_fail | **failed** | **media_disconnect** |
| 7 | normal | completed | completed |
| 8 | cancel | completed | completed |
| 9-10 | normal | completed | completed |
| 11 | media_fail | **failed** | **media_disconnect** |
| 12 | normal | completed | completed |
| 13 | cancel | completed | completed |
| 14-15 | normal | completed | completed |
| 16 | media_fail | **failed** | **media_disconnect** |
| 17 | normal | completed | completed |
| 18 | cancel | completed | completed |
| 19-20 | normal | completed | completed |
| 21 | media_fail | **failed** | **media_disconnect** |
| 22 | cancel | completed | completed |
| 23-24 | normal | completed | completed |

Every call reached the correct terminal state for its scenario, 24/24.
`[isolation]` check: 24 distinct tenants, 24 distinct `CallSession` ids, 24
distinct FreeSWITCH channel UUIDs, 24 distinct SIP `Call-ID`s -- **no
identity reuse across unrelated calls**, real IDs read from the real
database, not asserted. Every subsequent call after every failure started
and completed normally (see the table -- no failure was ever followed by a
stuck or misbehaving next call).

A small number of `normal`/`cancel` calls came back `own_reply_present=False`
(STT transcribed e.g. "the code word is also" instead of "...alpha...") --
this is the **exact, already-established** Phase 2.28 STT-variability
finding (brief section 10, not reopened): real Deepgram STT over a
synthesize -> 8kHz PCMU -> re-transcribe round trip is not word-perfect even
with zero concurrency. It never affected classification (every such call
still reached `status=completed`) and never involved a wrong keyword (no
cross-contamination).

**Result: 24 real sequential calls achieved** -- meets the brief's stated
minimum (20+) but falls short of its "preferably 30+" target. This was a
deliberate, time-boxed choice given this phase's own real-call wall-clock
cost (~21s/call average, ~500s total for this one sequence) balanced
against the number of distinct real-staging scenarios the brief also
requires (sections 4, 6, 7, 9); reported honestly rather than padded.

## 4. Repeated successful calls -- resource measurements -- REAL STAGING, PARTIAL

Sampled at the brief's own requested checkpoints (before first call, after
5, 10, 20, and the final call) during the section 3 soak above:

| checkpoint | calls done | Python RSS | asyncio tasks | `CallRuntime._errors` size | active media streams | DB executor threads | wall elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- |
| before_first_call | 0 | 108.1 MiB | 6 | 0 | 0 | 0 | 1.2s |
| after_5_calls | 5 | 124.3 MiB | 6 | 0 | 0 | 2 | 119.4s |
| after_10_calls | 10 | 124.1 MiB | 7 | 1 | 0 | 2 | 220.7s |
| after_20_calls | 20 | 124.9 MiB | 7 | 3 | 0 | 2 | 420.9s |
| after_24_calls (final) | 24 | 127.9 MiB | 6 | 4 | 0 | 2 | 499.7s |

* **Python RSS**: rose ~16 MiB in the first 5 calls (consistent with
  one-time import/connection-pool/JIT-adjacent warm-up, not per-call
  growth), then stayed essentially flat (124.1 -> 124.9 -> 127.9 MiB) over
  the remaining 19 calls -- a ~3 MiB drift across 19 calls that this
  sample cannot distinguish from ordinary allocator fragmentation versus a
  slow leak. **This is evidence of approximate stability across 24 calls,
  not proof of the absence of a leak** -- stated explicitly per the
  brief's own section 12 instruction, and doubly so given section 1's
  confirmed leak in a *different* structure (`CallRuntime._errors`) that
  this measurement window was too short to see reflected in RSS at all.
* **asyncio task count**: stayed in a narrow 6-7 band throughout -- no
  growth.
* **Active media streams/sockets** (`FreeSwitchMediaProvider._streams` /
  `._sockets`, read directly): **0 at every checkpoint**, including after
  24 calls and 4 induced media failures -- strong, direct evidence that
  per-call media session bookkeeping is fully released between calls, with
  no accumulation from either normal completion or induced failure.
* **DB executor threads** (`DatabaseBoundary._executor._threads`, read
  directly): 0 before any call, then a steady 2 from the first checkpoint
  onward (well under the pool's own `max_workers=8` cap) -- no unbounded
  thread growth.
* **`CallRuntime._errors` size**: see section 1 -- the one metric in this
  table that is a confirmed, monotonic, uncapped counter, not stable.
* **FreeSWITCH container CPU/mem** (`docker stats p224-freeswitch`):
  sampled throughout the concurrent trials (section 6) in the 1.4%-18.3%
  CPU / ~60-65 MiB range, consistent with Phase 2.27's own prior
  measurements -- no drift observed, though this soak's own sequential
  (not concurrent) design means FreeSWITCH's own load per sample point was
  low and not a stress test of its own resource behavior.

**Classified PARTIAL**, not GREEN: a single 24-call sample from one process
run is meaningful evidence of short-run stability for every metric except
`CallRuntime._errors`, but is explicitly not a long-run leak-absence proof
for any of them -- consistent with the brief's own worked example ("a
stable 30-call RSS sample is evidence of stability, not proof of absence
of leaks").

## 5. Repeated failure -> recovery -- REAL STAGING + HERMETIC, GREEN (functional property)

**Real staging**: the section 3 soak above *is* this scenario --
`normal -> ... -> media_fail -> normal -> cancel -> normal -> normal ->
media_fail -> ...` repeated four times over on one long-lived runtime.
After every one of the 4 induced media failures: the failed `CallSession`
reached a real terminal status (`failed`/`media_disconnect`), the very
next call in the sequence started and completed normally, and a
*different* tenant completed a call immediately afterward every time (each
call in this soak uses its own freshly-provisioned tenant, so "the next
call succeeds" and "a different tenant succeeds after a failure" are the
same, continuously-reconfirmed observation 20 times over across this one
sequence). No duplicate finalization was observed (each `CallSession` in
the per-call table above has exactly one terminal row) and no lingering
task was left over (`CallRuntime.current_load` returned to a value
consistent with "no leaked task" between every pair of calls -- the
soak script asserts this is 0 or 1 -- never accumulating -- at every
per-call sample point in its own JSON output).

**Cancellation caveat, stated honestly.** The brief's "failure type B" is
"call cancellation during active AI/media processing" -- meaning a real,
live, *supervisor-initiated* `CallRuntime.cancel_call()` while a real call
is mid-processing. `_run_call_on_shared_infra()` (the shared real-call
helper this phase reused rather than rewriting) has no hook to trigger
that specific call from outside while a real SIP call is in flight; adding
one would have meant modifying that shared helper, which this phase's own
brief says to avoid unless required. This phase's real-staging `cancel`
scenario instead reused Phase 2.28's own established analog -- a real
early caller `BYE` during active processing -- which is a genuine live
test of the same "must reach a real terminal state after a mid-processing
interruption" property, but not the identical trigger. **Genuine
supervisor-initiated cancellation mid-processing is fully covered, but
only hermetically**, by the pre-existing
`test_hangup_mid_stalled_ai_turn_leaves_a_concurrent_call_unaffected` (not
reopened or modified) and by this phase's own new
`test_repeated_failures_do_not_poison_later_calls` (below). This is an
explicit, stated limitation, not a silent gap.

**New hermetic test**:
`tests/integration/test_runtime_integration.py
::test_repeated_failures_do_not_poison_later_calls`. Runs the brief's own
suggested sequence -- `normal, failure, normal, failure, failure, normal,
normal` -- one call at a time on a single `CallRuntime` instance (never
restarted), using a broken-engine failure trigger (the same shape as the
pre-existing, not-reopened `test_one_call_failing_does_not_affect_others`).
Polls for state transitions (`error_for()`, `is_running()`,
`CallSession.status`, `runtime.current_load`) instead of a single fixed
`asyncio.sleep()`, specifically to avoid reproducing this file's own
documented fixed-sleep race pattern (brief section 10: not reopened).
Confirmed stable across 3 consecutive local runs. Does **not** assert
anything about `CallRuntime._errors`'s own long-run size -- see section
1's own note on why encoding the current (buggy) growth behavior as an
expected test outcome was deliberately avoided.

## 6. Concurrent failure recovery -- REAL STAGING, GREEN

Reused the existing, unmodified `scripts/validate_staging_concurrent_media_e2e.py`
(Phase 2.29's own tool) directly -- it already builds the identical
one-shared-runtime topology for a *concurrent* batch, which is exactly
this section's own question.

* **2-call trial, call 1 fails** (`--calls 2 --stop-media-call-index 1`):
  call 1 reached `status=failed, end_reason=media_disconnect`; call 2
  reached `status=completed, end_reason=completed`, own reply verified,
  no cross-contamination. 2 distinct tenants/sessions/FreeSWITCH UUIDs.
* **2-call trial, call 2 fails** (`--calls 2 --stop-media-call-index 2`,
  see section 6.1 for a test-harness artifact found and resolved on the
  first attempt at this exact trial): call 2 reached
  `status=failed, end_reason=media_disconnect`; call 1 reached
  `status=completed, end_reason=completed`. 2 distinct
  tenants/sessions/FreeSWITCH UUIDs.
* **3-call trial, call 2 fails, calls 1 and 3 continue**
  (`--calls 3 --stop-media-call-index 2`): call 2 reached
  `status=failed, end_reason=media_disconnect`; calls 1 and 3 both reached
  `status=completed, end_reason=completed`. 3 distinct
  tenants/sessions/FreeSWITCH UUIDs.

Every trial: the shared ESL connection, media listener, and `CallRuntime`
were never killed or restarted; only the one selected call's own
FreeSWITCH channel was touched by the induced failure
(`api uuid_audio_stream <uuid> stop`, scoped to that channel alone, Phase
2.29's own mechanism, unmodified). No trial showed cross-call
contamination (no call ever heard another call's own distinguishing
keyword) and no trial showed the failed call's own cleanup affecting any
other call's state.

### 6.1 A test-harness artifact found, diagnosed, and ruled out

The **first** attempt at the "2-call trial, call 2 fails" scenario (run
immediately after the "call 1 fails" trial, reusing that trial's own
default local SIP/RTP port numbers) produced an anomalous result: call 2's
induced media disconnect reached `status=completed` instead of `failed`,
and **both** calls' locally-captured RTP audio came back as 0 bytes
(normally only the failed call's own capture is empty).

This was investigated, not waved away. Reading `p224-freeswitch`'s own log
for that exact FreeSWITCH channel UUID showed:

```text
stream_session_cleanup: no bug - websocket connection already closed
```

-- i.e. by the time the script's own explicit `stop` ESL command reached
FreeSWITCH, the media websocket was *already* closed, before the "induced"
failure was even issued. Combined with **both** calls' RTP capture (a
completely separate signal path from the media websocket) coming back
empty, this points to a local test-harness artifact: the two trials ran
back-to-back reusing the exact same default local SIP/RTP port numbers,
and the first trial's own local UDP sockets had not yet been released by
the OS when the second trial's UAC tried to bind the same ports.

**Re-running the identical scenario with a distinct local port range**
(`--local-sip-port 15850 --local-rtp-port 15950`) reproduced the expected,
correct result immediately: call 2 `failed`/`media_disconnect`, call 1
`completed`/`completed`, non-zero captured audio on the untouched call.
The 3-call trial above and every other real-staging invocation in this
phase used a distinct local port range per invocation specifically to
avoid this artifact going forward. This is recorded here as a
methodological finding for future real-staging scripts in this repository
(distinct `--local-sip-port`/`--local-rtp-port` per back-to-back
invocation), not as a product defect -- the underlying product code
(`voiceagent/`) was never involved in this artifact at all.

## 7. Recovery after multiple failures -- REAL STAGING + HERMETIC, GREEN

The section 3 soak's own sequence contains two back-to-back-ish failure
clusters relative to the brief's suggested shape (four total failures
spread across 24 calls, each immediately followed by normal calls that
all succeeded) and the new hermetic test (section 5) runs the brief's
*exact* suggested sequence, including two failures with no successful
call between them (`failure, failure`), on one never-restarted
`CallRuntime`. Both show the runtime continuing to accept and complete
calls after multiple independent failures, with no instance of the
runtime becoming unusable -- so no scenario in this phase required
stopping and classifying as FAILED per the brief's own escalation-safety
instruction (section 7 of the brief).

## 8. Resource-leak investigation -- see sections 1 and 4

Section 1 is this phase's own answer to this section of the brief: a
concrete, reproducible, monotonic leak was found
(`CallRuntime._errors`). Section 4 covers every other measured resource
(RSS, asyncio tasks, media streams/sockets, DB executor threads,
FreeSWITCH container CPU/mem), all showing short-run stability with the
stated PARTIAL caveat. No intrusive new production instrumentation was
added -- every measurement in this phase reads existing, already-private
attributes (`_tasks`, `_errors`, `_streams`, `_sockets`,
`_executor._threads`) directly from validation-only scripts/tests, never
from new logging, metrics, or production code paths.

## 9. Security / isolation -- REAL STAGING, GREEN

Reconfirmed across every real-staging scenario in this phase (24 sequential
calls, section 3; 3 concurrent trials, section 6):

* **Tenant isolation**: every call in every scenario used its own freshly
  provisioned tenant (`create_tenant()` per call, matching Phase
  2.27/2.28/2.29's own convention) -- 24 + 2 + 2 + 3 = 31 distinct real
  tenants created and used across this phase's own real-staging work, zero
  reuse.
* **`CallSession` isolation**: every call's own `CallSession` id was
  distinct and read from the real database, never asserted -- confirmed in
  every trial's own `[isolation]`/`[cross-call]` check.
* **Media-ticket binding / FreeSWITCH UUID / SIP `Call-ID` correlation**:
  every call's own FreeSWITCH channel UUID and SIP `Call-ID` were
  distinct and correctly correlated to that call's own `CallSession` row
  in every trial -- unchanged mechanism from Phase 2.21/2.29, re-observed,
  not re-designed.
* **No cross-call transcript/audio leakage**: no trial in this phase (24
  sequential + 5 concurrent, 29 total real calls this phase alone) ever
  showed one call's own transcript containing another concurrent call's
  distinguishing keyword.
* **Failure of one tenant's call did not affect another tenant's call**:
  demonstrated directly in every concurrent trial (section 6) and
  continuously across the sequential soak (section 3) -- every call
  immediately following an induced failure, for a different tenant, in
  the same shared runtime, completed normally.
* **Failed-call cleanup did not touch another tenant's active state**:
  the only per-call side effects observed on failure were scoped to that
  call's own `CallSession` row, its own FreeSWITCH channel, and (section
  1) one entry in `CallRuntime._errors` keyed by that call's own unique
  id -- never another call's data.

## 10. Findings not reopened

Per the brief's own section 10, none of the following were reopened or
altered:

* `"alpha"` -> `"also"` (and this phase's own newly-observed instance,
  `"alpha"` -> `"the code word is also"`, section 3): STT recognition
  variability, established Phase 2.28, not a concurrency/media-integrity
  defect.
* Media disconnect classification (`TransportError` -> `"media_disconnect"`
  -> `CallSession` `failed`): re-confirmed correct in every real-staging
  trial this phase ran (24 sequential + 3 concurrent = 8 induced media
  disconnects total, 8/8 correctly classified once the section 6.1
  test-harness artifact was accounted for). `voiceagent/runtime/call_task.py`
  was not modified.
* `test_one_call_failing_does_not_affect_others` and
  `test_run_call_task_happy_path_completes_and_finalizes`: not modified.
  The former's known fixed-sleep race reproduced again in this phase's own
  final integration-suite run (section 13) -- confirmed, once more, to be
  pre-existing and unrelated to any change in this phase (it was already
  independently confirmed against a clean parent commit in the session
  that produced Phase 2.29's own checkpoint commit).

## 11. Limitations, stated honestly

* See section 1a's own "Remaining limitation, stated honestly" for the
  `CallRuntime._errors` fix's specific trade-off (bounded by call cadence,
  not by an absolute time/count).
* 24 real sequential calls achieved, not 30+ -- see section 3's own note.
* Genuine `CallRuntime.cancel_call()`-initiated cancellation was not
  re-exercised against the real SIP stack in this phase; only hermetically
  (pre-existing test, unmodified) and via a real-staging caller-BYE analog
  -- see section 5's own note.
* Resource measurements are a single 24-call sample from one process run;
  they are evidence of short-run stability, not a long-run leak-absence
  proof, for every metric except `CallRuntime._errors` (section 1, which
  *is* conclusively shown to be unbounded, not merely "not yet shown
  stable").
* FreeSWITCH's own resource behavior was sampled during concurrent trials
  (section 6) but not under sustained, high-concurrency load -- this phase's
  own soak was sequential by design (topology requirement, section 2), so
  it does not stress-test FreeSWITCH's own resource ceiling the way a
  larger concurrent batch (Phase 2.27's own territory) would.
* One transient, self-diagnosed test-harness artifact occurred and is
  documented in full (section 6.1); a second, unrelated transient
  collision (a `PhoneNumberUnavailableError` in an unrelated hermetic
  integration test, `test_conversation_integration
  .py::test_get_conversation_route_404s_for_a_foreign_call`) occurred once,
  during this phase's own integration-suite run, because this phase's own
  real-staging soak was still writing to the same real database
  concurrently at that moment (both this soak's own fixture helper and
  that hermetic test independently generate a random 7-digit E.164 number
  in the same namespace). Re-running that one test in isolation, and then
  the full integration suite once the soak had finished, both passed
  cleanly (section 13) -- confirmed as a self-inflicted scheduling
  overlap in this phase's own validation work, not a product defect.

## 12. Results matrix

```text
Section 1  Resource-leak investigation (CallRuntime._errors)   REAL STAGING, FAILED -> FIXED, GREEN (see section 1a)
Section 3  Sequential long-running soak (24 real calls)        REAL STAGING, GREEN
Section 4  Resource measurements (RSS/tasks/streams/threads)   REAL STAGING, PARTIAL
Section 5  Repeated failure -> recovery (functional property)  REAL STAGING + HERMETIC, GREEN
Section 5  Genuine supervisor cancellation mid-processing      HERMETIC only, GREEN; NOT VALIDATED against real stack
Section 6  Concurrent failure recovery (2x 2-call, 1x 3-call)  REAL STAGING, GREEN
Section 7  Recovery after multiple/back-to-back failures       REAL STAGING + HERMETIC, GREEN
Section 9  Security / tenant / session / media isolation       REAL STAGING, GREEN
```

## 13. Validation gates

Gates below are from the remediation session (section 1a), run after the
`CallRuntime._errors` fix; the original Phase 2.30 session's own gate run
(pre-fix) is preserved in this same order for reference where it differs.

* Targeted Phase 2.30/2.30-remediation tests
  (`tests/integration/test_runtime_integration.py
  ::test_repeated_failures_do_not_poison_later_calls`,
  `tests/runtime/test_supervisor.py` in full, including the 6 new
  regression tests from section 1a): **all passed**.
* Hermetic `pytest` (default `addopts`, excludes `-m integration`): **958
  passed, 261 deselected** (952 from the original session plus the 6 new
  `tests/runtime/test_supervisor.py` regression tests from section 1a;
  261 deselected, not 260, because the Phase 2.30-added integration test
  is itself marked `integration` and correctly deselected from this run).
* Real integration suite (`pytest -m integration`, real PostgreSQL):
  **260 passed, 1 failed** -- `test_one_call_failing_does_not_affect_others`,
  the known, documented, pre-existing fixed-sleep race (not reopened, not
  modified; confirmed unrelated to both this phase's original work and
  this remediation -- independently reproduced against a clean parent
  commit earlier in this session, before either began).
* Real-staging re-verification of the fix itself: two focused runs against
  `p224-freeswitch` and the real Postgres (section 1a) -- both exit code 0,
  both showing bounded `_errors` growth, correct `media_disconnect`
  classification, correct cancellation behavior, and full identity
  isolation.
* Ruff check: **all checks passed**.
* Ruff format: **357 files already formatted**.
* Pyright: **0 errors, 0 warnings, 0 informations**.
* import-linter: **8 contracts kept, 0 broken**.
* detect-secrets (`.secrets.baseline`): scan's own known in-place
  mutation reproduced again (pre-existing findings across files entirely
  outside this phase's/remediation's own changes, matching Phase 2.29's
  own documented precedent exactly) -- diffed, confirmed no new findings
  in any file touched by either the original phase or this remediation,
  reverted with `git checkout -- .secrets.baseline` before ever being
  staged.
* pip-audit: **no known vulnerabilities** (`saas-os`/`voiceagent` skipped,
  not on PyPI, expected and unchanged from every prior phase).
* `git diff --check`: **clean**.

## 14. Files changed

```text
scripts/validate_staging_long_running_soak.py (new, original session)  -- sequential real-call
                                                          soak harness, resource sampling, reuses
                                                          validate_staging_concurrent_media_e2e.py's
                                                          own fixture/call helpers
tests/integration/test_runtime_integration.py (extended, original session) -- one new hermetic
                                                          test, _wait_until() polling helper;
                                                          no existing test modified
docs/PHASE-2.30-LONG-RUNNING-STABILITY.md (new, then extended by section 1a) -- this document
voiceagent/runtime/supervisor.py (extended, remediation session) -- _reap_terminal_errors(),
                                                          called from start_call(); see section 1a
tests/runtime/test_supervisor.py (extended, remediation session) -- 6 new regression tests;
                                                          no existing test modified; see section 1a
```

**Production `voiceagent/` files changed: exactly one --
`voiceagent/runtime/supervisor.py`**, in the remediation session, to fix
the one genuine defect this phase found (section 1a). No other production
file was touched, in either session.
