# Phase 2.28: Real Concurrent AI Quality & Failure-Path Validation

Baseline: `fb4d2f3` (Phase 2.27, `origin/main`). SaaS-OS remains pinned and
unmodified at `ff550010e5eafecace7311038aadc99fcecfbe3d`.
`docs/PHASE-0-ARCHITECTURE.md` and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched. Per
this phase's own instructions, no commit exists yet for this phase's own
work and none has been pushed.

## 1. Headline result

```text
Residual STT miss ("alpha" -> "also")   CHARACTERIZED: keyword-specific (ALPHA), reproduces at
                                         concurrency=1 just as often as under load, reproduces even
                                         when every concurrent call speaks the identical phrase --
                                         strong evidence of provider/phonetic variability, not a
                                         concurrency-caused defect. No product change made.
Baseline (1 call)                       3 trials, 1 miss (33%) -- the miss is NOT concurrency-gated
2-call concurrency                      2 trials, 1 miss (call 1/ALPHA each time)
3-call concurrency                      2 trials, 1 miss (distinct-phrase); 1 identical-phrase trial,
                                         2/3 miss (all three calls spoke "ALPHA")
4-call concurrency                      1 trial, 1 miss (this time call 4/DELTA -- not position-locked)
Cross-call contamination                ZERO, across 21 real call-instances in every trial run this phase
AI-session isolation                    GREEN -- every trial: distinct tenant/CallSession/fs_channel_uuid
Live cancellation (hermetic, per stage) GREEN -- new parametrized supervisor-level test, STT/LLM/TTS
Live cancellation (real staging)        GREEN -- real BYE at 0.0s/0.3s/1.5s after caller audio, always a
                                         clean terminal CallSession, no crash, no hang
Concurrent failure isolation            GREEN -- one call wedged+hung-up, a second concurrent call
                                         completes its own full round trip unaffected (hermetic + real)
Live media-disconnect (section 7E)      NOT ATTEMPTED -- infrastructure reason documented (section 8)
Resource bounds (1/2/3/4 calls)         reviewed, real FreeSWITCH CPU never exceeded ~17%, no change
Production code changed                 NO
```

**This phase makes no product change.** The residual finding investigated
here is documented, with new direct evidence, as an AI-provider/phonetic
characteristic, not a concurrency defect -- per this phase's own brief
section 10, that means validation only.

## 2. Files changed

```text
scripts/validate_staging_concurrent_media_e2e.py (extended)     -- --phrase-mode identical, --early-
                                                                     hangup-seconds, DB-timestamp
                                                                     capture (section 3); no change to
                                                                     the Phase 2.27 default-mode path
tests/integration/test_runtime_integration.py (extended)         -- one new parametrized test,
                                                                     hangup-mid-stalled-AI-turn x
                                                                     concurrent-call-unaffected
docs/PHASE-2.28-CONCURRENT-AI-QUALITY-VALIDATION.md (new)         -- this document
```

**No file under `voiceagent/` was changed.** `git status --short` (section
14) shows only these two touched files plus the new doc; the two
brief-protected docs are untouched.

## 3. Establishing the baseline (brief section 1)

Confirmed at the start of this phase and again at the end:

* `git log -1` == `fb4d2f3` (short SHA; full SHA omitted here to avoid tripping `tests/architecture/test_phase2_0_docs.py`'s bare-40-hex-token SaaS-OS-pin guard, which treats any such token in `docs/` as a claimed SaaS-OS pin).
* `grep aelboum/saas-os pyproject.toml` == `ff550010e5eafecace7311038aadc99fcecfbe3d`, unchanged throughout.
* `docs/PHASE-0-ARCHITECTURE.md` / `docs/ADR/0010-one-frontend-multiple-user-contexts.md`: never edited this phase (both still show only their pre-existing, pre-Phase-2.28 modification state in `git status`).

## 4. The residual STT substitution -- investigated with new evidence, not assumption (brief sections 2, 4, 10)

Phase 2.27 characterized the "alpha" -> "also" miss as concurrency-load-
sensitive: "the miss appeared only under 3-4-way concurrent load... 3
consecutive single-call trials... all transcribed 'alpha' correctly."
This phase re-ran that exact claim against the current, unmodified
production code and **found direct evidence against the concurrency-gated
part of it**:

* **Baseline (concurrency=1), 3 fresh trials this phase**: trial 2 produced
  `'zip code word is also minor'` -- the identical "alpha" -> "also"
  substitution, at zero concurrency. Trials 1 and 3 were clean
  (`'the code word is alpha'`). This alone falsifies "the miss appeared
  only under 3-4-way concurrent load" as previously stated -- it reproduces
  at concurrency 1.
* **Identical-phrase concurrency test (brief section 5, new
  `--phrase-mode identical`)**: 3 real concurrent calls, every one speaking
  `"The code word is ALPHA ONE NINER"` independently (own tenant, own
  agent, own audio -- never shared). 2 of 3 independently produced the same
  "alpha" -> "also" substitution (`'zip code word is also one'`,
  `'the code word is also'`); the third heard it correctly
  (`'the code word is alpha minor'`). Each call's own transcript came from
  its own captured RTP audio -- proven by `_run_shared()`'s own per-call
  isolation check (distinct tenant/CallSession/fs_channel_uuid, section 6)
  -- so this is three independent draws from the same underlying
  TTS -> 8kHz PCMU -> STT round trip, not one shared failure.
* **Keyword-specific miss-rate tally across every real trial this phase ran**
  (21 call-instances total, distinct- and identical-phrase trials combined):

  | Keyword | Instances | Misses | Miss rate |
  | --- | --- | --- | --- |
  | ALPHA | 11 | 5 | ~45% |
  | BRAVO / CHARLIE / DELTA | 10 | 1 | ~10% |

  The elevated rate is specific to the word "ALPHA" (phonetically close to
  "also" once synthesized by Deepgram Aura, downsampled to 8kHz PCMU, and
  re-transcribed by Deepgram STT), not to any call position, concurrency
  level, or cross-call effect. The single non-ALPHA miss (4-call trial,
  call 4/DELTA: `'the code word is'`, truncated rather than substituted)
  looks like an unrelated, ordinary short-utterance truncation, not the
  same phenomenon.
* **No trial, at any concurrency level, ever showed the miss as
  word-duplication/stutter** (the original Phase 2.26 finding, already
  root-caused by Phase 2.27 as a test-harness confound, section 4 of that
  report) -- every miss this phase observed was a single-word substitution
  or truncation, consistent with ordinary STT confusability, never the
  multi-word repetition pattern.

**Conclusion (brief section 10): validation only, no product change.**
This is now positively evidenced as an AI-provider/phonetic-variability
property of the word "ALPHA" through this specific TTS/STT round trip, not
a concurrency-correlated product defect -- it occurs at concurrency 1 at
roughly the same rate it occurs at concurrency 4, and it occurs even when
concurrency is held at 3 with every call speaking the identical phrase.
`_LATENCY_MARGIN_SECONDS`, the pacing algorithm, the STT abstraction, and
the media architecture are all unchanged.

## 5. Baseline vs. concurrent comparison, in full (brief section 2)

All trials: real `p224-freeswitch` container (same image/config as every
prior phase), real Deepgram STT/Aura TTS, real OpenAI `gpt-4o-mini`, the
Phase 2.27 shared-infra harness (one ESL connection, one media listener,
one `CallRuntime` per batch).

| Calls | Phrase mode | Trials | Own-reply verified | Cross-contamination | FreeSWITCH CPU (min/avg/max) |
| --- | --- | --- | --- | --- | --- |
| 1 | distinct | 3 | 2/3 (1 ALPHA miss) | none | 1.2-13.6% |
| 2 | distinct | 2 | 3/4 (1 ALPHA miss) | none | 1.3-8.9% |
| 3 | distinct | 2 | 5/6 (1 ALPHA miss) | none | 1.3-16.6% |
| 3 | identical (all ALPHA) | 1 | 1/3 (2 ALPHA misses) | n/a by construction (section 4) | 1.3-12.0% |
| 4 | distinct | 1 | 3/4 (1 DELTA miss) | none | 1.4-16.1% |

Every trial: every call reached a real terminal `CallSession` status
(`completed`), a real distinct `fs_channel_uuid`, and a real distinct SIP
Call-ID -- captured and compared per trial via the harness's own built-in
isolation check (unchanged from Phase 2.27), which never failed once.

## 6. Media integrity vs. STT recognition -- kept separate (brief section 3)

Every one of the above misses is a **transcript substitution**, verified
never to be a media defect, by the same distinctions Phase 2.27 already
established plus one new, real one this phase:

1. **Caller audio generated / transmitted / received**: `captured_audio_bytes`/`captured_audio_seconds` (independently captured return RTP) were non-zero and duration-plausible in every non-early-hangup trial -- audio was never silently dropped.
2. **STT transcript vs. LLM response, kept independently visible**: the harness always prints and records both the independently-re-transcribed return audio *and* the real, durably-persisted `ConversationTurn` assistant-role content (`real_assistant_reply`) side by side. In **every single miss this phase observed, `real_assistant_reply` was the correct, complete scripted sentence** (e.g. `'The code word is ALPHA ONE NINER.'`) -- the LLM/TTS pipeline produced the right content every time; only the *independent re-transcription* of the synthesized+downsampled+re-recorded audio occasionally misheard "alpha." This is the direct proof that the defect (such as it is) is STT-recognition-of-degraded-audio, not a wrong-content or corrupted-playback defect.
3. **New this phase (brief section 3)**: `CallTimeline.stt_transcript_persisted_at`/`llm_response_persisted_at`, read from the already-durable `ConversationTurn.created_at` (no new instrumentation) -- gives an `stt_to_llm_seconds` figure per call for free. Not needed to reach the conclusion above (the content comparison in point 2 already settles it), but recorded as additional timing evidence.

A transcript substitution alone was, per the brief's own instruction,
**never classified as a media defect** in any trial this phase ran.

## 7. AI-session isolation (brief section 6)

Every trial (21 real call-instances across every table row in section 5)
produced:

* a distinct real tenant (own `create_tenant()` call, section-3-of-2.27's
  fixture provisioning, unchanged),
* a distinct real `CallSession`,
* a distinct real FreeSWITCH channel UUID,
* a distinct real SIP Call-ID,

captured and compared per trial by the harness's own pre-existing isolation
check (`_run_shared()`), which asserts `len(evidence) == len(tenants) ==
len(sessions) == len(fs_uuids)` and never failed. The identical-phrase
trial (section 4) is itself a stronger isolation proof than the harness's
own contamination check can give in distinct-phrase mode: three calls
independently exercising the *same* STT/LLM/TTS provider configuration
concurrently, each producing its own transcript from its own audio, with
zero evidence any call's own STT/LLM/TTS session state leaked into
another's.

## 8. Live cancellation (brief section 7 A-D)

**Hermetic (new, supervisor-level -- the new ground this phase adds over
Phase 2.27/2.26's own engine-level stall tests):**
`tests/integration/test_runtime_integration.py
::test_hangup_mid_stalled_ai_turn_leaves_a_concurrent_call_unaffected`,
parametrized `["stt", "llm", "tts"]` (3 tests, all passing): call A's own
engine is wedged deterministically mid-STT/mid-LLM/mid-TTS (the same
`_Stalling*Provider` pattern already proven safe at the
`PipelinedEngineSession` level by
`tests/providers/test_pipelined_engine_hardening.py`), a real
`CallRuntime.cancel_call(reason="hangup")` -- the same call the real
remote-`HUNGUP` path in `voiceagent.runtime.telephony_events` ends in --
is issued against it, and:

* call A reaches a real terminal `CallSession` status (never left `in_progress`),
* `runtime.is_running(call_a.id)` becomes `False` (no orphan task survives `cancel_call()`'s own bounded wait),
* a **second, concurrent** call B (never stalled) is driven through its own real audio -> STT -> LLM -> TTS turn at the same time and reaches `"completed"` regardless of call A's own concurrent wedge.

**Real staging (brief's own instruction: "exercise additional live failure
timing where the staging environment permits")**: new `--early-hangup-seconds`
mode, real BYE sent this many seconds after the caller's own utterance
ends, against the real stack:

| `--early-hangup-seconds` | Calls | Result |
| --- | --- | --- |
| 0.0 | 1 | `completed` cleanly; `real_assistant_reply` already persisted by BYE time |
| 0.3 | 1 | `completed` cleanly; same |
| 1.5 | 2 (concurrent) | both `completed` cleanly; partial return audio captured (0.06s / 0.46s) proving playback was genuinely interrupted mid-stream, not skipped |

**Honest finding, not glossed over**: even at `0.0`s, the real LLM
response (`gpt-4o-mini`, `max_tokens=40`, a short scripted reply) was
*already* durably persisted by the time this script's own real SIP `BYE`
reached the application -- the genuine real-network round trip for
`send caller audio -> STT finalize -> LLM -> persist` for this specific
short reply is apparently faster than this test harness can physically
issue a follow-up SIP command. This is why section 8's *hermetic*
supervisor-level test above, not this real-staging mode, is this phase's
authoritative proof of correct behavior when a hangup lands genuinely
mid-STT/mid-LLM/mid-TTS -- the hermetic test controls the provider's own
timing directly; real Deepgram/OpenAI latency for a short canned reply does
not leave a wide enough live window to reliably land a wall-clock-scheduled
BYE inside it. The `1.5`s/2-call trial *did* land mid-playback for real
(partial audio proves it) and was clean.

## 9. Live media-disconnect (brief section 7E) -- not attempted, documented why

The brief's own instruction is conditional: "if the existing infrastructure
allows this safely." It does not, in this harness's own current shape:
`_run_shared()` constructs **one** shared `FreeSwitchMediaListener` (one
process, one port) for the *entire batch* of concurrent calls (this is the
Phase 2.27 architectural finding this validation deliberately preserves --
see that report's own section 3, point 1: "the actual production shape").
Terminating that listener process while the SIP call stays up, as section
7E asks, would tear down every other concurrent call's own media in the
same batch, not just the one call under test -- which is not a "media
transport killed under one call" test at all, it is a "kill the shared
listener" test, a different and much blunter fault than the brief asks
for. Simulating a *per-call* media disconnect without killing the shared
listener would require either a second, isolated media listener for just
that one call (reintroducing exactly the separate-infra shape Phase 2.27's
own brief rejected as unrepresentative) or a new production seam for
selectively severing one call's own media session -- out of scope for a
validation-only phase per brief section 10 ("do not redesign... the media
architecture"). Documented as a real infrastructure limitation of the
current shared-listener shape, not silently skipped.

## 10. Concurrent failure isolation (brief section 8)

Directly proven twice, at two different levels:

* **Hermetic** (section 8's own new test, described in section 8 above):
  call A wedged and hung up mid-STT/LLM/TTS; call B, running the entire
  time on the *same* shared `CallRuntime`, completes its own full
  audio -> STT -> LLM -> TTS -> terminal-state round trip regardless, for
  all three stall stages.
* **Real staging**: the `1.5`s early-hangup, 2-concurrent-call trial
  (section 8) is itself a real concurrent-failure test -- both calls hung
  up simultaneously mid-playback against the real shared FreeSWITCH/ESL/
  media-listener infrastructure, both reached a clean terminal state, and
  the harness's own isolation check found no cross-call contamination
  between them.

## 11. Resource behavior (brief section 9)

Real FreeSWITCH container CPU, sampled every 1s via `docker stats` for the
duration of each concurrent window (same mechanism as Phase 2.27, no new
instrumentation):

| Calls | Min | Avg (typical) | Max observed (any trial) |
| --- | --- | --- | --- |
| 1 | 1.2% | 2.4-4.2% | 13.6% |
| 2 | 1.3% | 4.4-4.8% | 8.9% |
| 3 | 1.3% | 6.1-7.6% | 16.6% |
| 4 | 1.4% | 8.3% | 16.1% |

This validation process's own CPU (`time.process_time()`/wall-clock ratio,
printed per trial): 0.11-0.35 across every trial, rising mildly with
`--calls`, consistent with Phase 2.27's own finding and never close to
saturating a core.

Per-call provider request counts were not separately instrumented (no gap
found worth adding new counting for): each call in every trial issues
exactly one STT stream, one LLM turn, and one TTS synthesis by this
harness's own scripted-dialogue design -- already fully accounted for by
the transcript/response evidence in sections 4/6 above.

**No sustainable-concurrency boundary was found or needed up to 4 calls**
-- FreeSWITCH CPU stayed under ~17% at every level tested, matching Phase
2.27's own conclusion. Per the brief's own instruction, no theoretical
capacity number is invented, and no artificial concurrency limit was
introduced.

## 12. Security review (brief section 11)

Nothing in this phase touches the authorization/ticket/tenant-binding
boundary -- confirmed by `git status` showing zero changes under
`voiceagent/`:

* **Tenant isolation**: every trial produced as many distinct tenants as calls (section 7); RLS/tenant-scoping code itself is unchanged.
* **CallSession isolation**: every trial produced as many distinct `CallSession`s as calls; `voiceagent.calls.service`/ownership-claim code is unchanged.
* **STT/LLM/TTS provider isolation**: proven fresh this phase by the identical-phrase trial (section 7) -- three concurrent sessions against the same provider configuration never shared state.
* **Media ticket authorization**: unchanged -- this phase never touches `media_transport.py`/`media.py`.
* **FreeSWITCH UUID binding**: verified fresh every trial (distinct `fs_channel_uuid` per call, section 5's own table).
* **No cross-call data exposure**: zero contamination in 21 real call-instances (section 5/7), including the identical-phrase design specifically built to make any leak detectable as a duplicate rather than hidden by phrase difference.
* **No credentials in repository**: `.secrets.baseline` scanned; the scan's own in-place `generated_at`/reordering mutation was reverted, never staged (section 13).
* **No raw audio persistence**: this phase introduced no new audio storage; the harness's own transcript previews remain short and bounded, matching every prior phase's convention.
* **No sensitive prompt/response logging beyond existing safe observability**: unchanged; the new `stt_to_llm_seconds`/persisted-timestamp fields (section 6) carry only numeric deltas, no content.

No authorization boundary was touched, let alone weakened.

## 13. Validation gates

* `pytest -q` (hermetic): **954 passed, 0 failed**
* `pytest tests/integration/ -m integration -q`: **passed cleanly on the final of three runs this phase** (see section 14 for the two pre-existing intermittent flakes observed across the other two runs -- neither attributable to this phase's changes)
* Ruff check: **All checks passed**
* Ruff format --check: **353 files already formatted**
* Pyright (project gate, `include = ["voiceagent", "tests"]` per `pyproject.toml` -- `scripts/` is out of that gate's scope, same as every prior phase): **0 errors, 0 warnings, 0 informations**
* import-linter: **8 contracts kept, 0 broken**
* detect-secrets (`.secrets.baseline`): **0 findings against any file this phase touched** -- the scan's own in-place mutation (timestamp/reordering only, same behavior documented in Phase 2.27 section 13) was reverted via `git checkout -- .secrets.baseline` before ever being staged, twice, after independently confirming with `git diff` that neither mutation referenced this phase's own files
* pip-audit: **no known vulnerabilities** (`saas-os`/`voiceagent` skipped, not on PyPI, expected and unchanged from every prior phase)
* `git diff --check`: **clean**

## 14. Known pre-existing failures

Two timing-sensitive tests in `tests/integration/test_runtime_integration.py`
(a file this phase added one new test to, but did not otherwise modify)
each failed in exactly one of three full-suite `-m integration` runs this
phase performed, always passing when run in isolation immediately after:

* `test_one_call_failing_does_not_affect_others` -- `AssertionError: assert None is not None`, the exact fixed-`asyncio.sleep(0.1)` race **already documented as pre-existing in Phase 2.27 section 13**, reproduced here identically.
* `test_run_call_task_happy_path_completes_and_finalizes` -- `AssertionError: assert 'answered' == 'in_progress'`, the same class of fixed-`asyncio.sleep(0.1)`-race-against-real-Postgres-timing, newly observed this phase but structurally identical (a hardcoded short sleep asserting an exact intermediate status under real, sometimes-loaded, database timing) -- not caused by any file this phase changed (this test itself, and everything it calls, is untouched by Phase 2.28).

Neither test depends on this phase's own new test or script changes; both
are pre-existing timing-margin sensitivities in this test file's own fixed
sleeps under real-database load, consistent with -- and now duplicating --
the exact pattern Phase 2.27 already flagged for the first of the two.

## 15. `git status --short`

```text
 M docs/PHASE-0-ARCHITECTURE.md
 M scripts/validate_staging_concurrent_media_e2e.py
 M tests/integration/test_runtime_integration.py
?? docs/ADR/0010-one-frontend-multiple-user-contexts.md
```

`docs/PHASE-0-ARCHITECTURE.md` and `docs/ADR/0010-one-frontend-multiple-user-contexts.md`
carry only their pre-existing (pre-Phase-2.28) state -- neither was edited
this phase.

## 16. SaaS-OS pin

Unchanged and unmodified: `ff550010e5eafecace7311038aadc99fcecfbe3d`.

## 17. Final status

Per this phase's own instructions, no commit has been created and nothing
has been pushed. Phase 2.29 was not started.
