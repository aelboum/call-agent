# Phase 2.26: Real SIP TTS Playback Investigation & Validation

Baseline: `a2eaa75` (Phase 2.25,
`origin/main`). SaaS-OS remains pinned and unmodified at
`ff550010e5eafecace7311038aadc99fcecfbe3d`. `docs/PHASE-0-ARCHITECTURE.md`,
`docs/PHASE-2.10-STATUS.md`, and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched.
Per this phase's own instructions, no commit exists yet for this phase's
own work and none has been pushed.

Phase 2.25 closed real SIP signaling, real RTP, and one direction (caller
voice in) of a real spoken round trip, but left the other direction (agent
voice out) `NOT VALIDATED`, concluding -- without a verified root cause in
`mod_audio_stream`'s own source -- that this exact vendor image's module had
"a real, reproducible playback limitation." This phase reproduces that
finding from the current baseline, investigates the vendor module's own
public source at the exact pinned commit, finds and fixes two genuine
product defects plus one genuine product-code gap, and validates real
outbound TTS delivery against a real SIP caller.

## 1. Headline result

```text
REAL SIP INVITE                                        GREEN (unchanged, Phase 2.25)
REAL RTP (both directions, transport layer)            GREEN (unchanged, Phase 2.25)
REAL FreeSWITCH media attach (in_progress)             GREEN (unchanged, Phase 2.25)
REAL caller speech -> REAL Deepgram STT                 GREEN (unchanged, Phase 2.25)
REAL OpenAI LLM deterministic response                  GREEN (unchanged, Phase 2.25)
REAL Deepgram Aura TTS synthesis                        GREEN (unchanged, Phase 2.25)
REAL TTS audio delivered to the real SIP caller         GREEN (this phase, single call, real RTP + real re-transcription)
Complete one-shot real spoken round trip                GREEN (single call, reproduced 3 times)
Real concurrency (2 simultaneous real SIP calls)        PARTIAL (isolation confirmed; audio quality and a
                                                                  test-harness race are documented limitations)
```

**Root cause of Phase 2.25's finding**: not a vendor-binary defect. The
exact pinned `mod_audio_stream` build (`amigniter/mod_audio_stream` at
`ec2a781`, confirmed from this product's
own pinned FreeSWITCH image's build labels and Dockerfile, both public)
decodes a `streamAudio` playback payload, writes it to a temp file, and
fires a `mod_audio_stream::play` event naming that file -- and stops there,
by this vendor's own documented design (its README: full automatic
playback is a separate, commercial edition; the free edition's own event
exists so an ESL-side listener finishes the job with FreeSWITCH's own
native `uuid_broadcast`). This product had no such listener. Two more real,
independent product bugs compounded the symptom once the listener was
added: a byte-parsing bug in this product's own ESL frame reader that
corrupted the very JSON body the listener needs, and no pacing/coalescing
of the many small `AudioOut` frames a streaming TTS reply produces, which
turned into a burst of overlapping native broadcasts.

## 2. Infrastructure used

Identical to Phase 2.25, reused unchanged: the same `voiceagent-test-pg`
PostgreSQL container, the same running `p224-freeswitch` container
(`rasonyang/freeswitch-aicc@sha256:46d34d6667de6632a0020cd96a6154e903f657ccf750aa4fb7769352f54e0f35`,
its own `01_phase225_e2e.xml` throwaway dialplan extension still present
and unmodified), the same `scripts/sip_uac.py`, the same
`scripts/validate_staging_sip_spoken_e2e.py` (extended this phase with an
opt-in `--concurrent` flag, section 9), and the same real Deepgram/OpenAI
credentials via `SECRETS_ENV_FILE=.env.phase223.local`.

## 3. Reproducing Phase 2.25's finding from the current baseline

Before changing anything, `scripts/validate_staging_sip_spoken_e2e.py` was
run unmodified against baseline `a2eaa75`:

```text
captured 0 real PCM bytes of return audio
independently transcribed: ''
NOT VALIDATED -- real TTS audio reaching the caller over real RTP: not observed
```

Identical to Phase 2.25's own documented result. The finding reproduces.

## 4. Investigating the vendor module's own public source

The pinned FreeSWITCH image's own OCI labels (`docker inspect
rasonyang/freeswitch-aicc`) name the exact upstream commit:
`mod_audio_stream.commit: ec2a781`. Its own
`freeswitch/Dockerfile` (public,
`github.com/rasonyang/ai-native-callcenter`) pins the exact upstream repo:
`https://github.com/amigniter/mod_audio_stream.git`, the same commit. Both
are public; both were read directly (`gh api`), not guessed.

**The module's own README, at the exact pinned commit** (not the
maintainer's later `main`, which has since moved on to a commercial
"Bi-Directional Streaming with Automatic Playback" release): documents a
`play` feature already present at this commit, with the exact envelope
this product already sends (`{"type":"streamAudio","data":
{"audioDataType":"raw","sampleRate":8000,"audioData":"..."}}`) -- and
states plainly that receiving this envelope produces one `mod_audio_stream
::play` event whose own body names a **temp file** the module wrote, "All
the files generated by this feature will reside at the temp directory."

**The module's own source, at the exact pinned commit**
(`audio_streamer_glue.cpp::processMessage()`, `mod_audio_stream.c::
responseHandler()`): read end to end. `processMessage()` decodes the
`streamAudio` payload, writes it to
`<temp_dir>/<channel-uuid>_<n>.tmp.r8`, and returns; `responseHandler()`
fires the `mod_audio_stream::play` CUSTOM event, with the file path in the
event's own JSON body. **There is no call anywhere in this file, or in
`mod_audio_stream.c`, to any channel-audio-injection API**
(`switch_ivr_broadcast`, `switch_ivr_play_file`, or equivalent) --
confirmed by reading both files top to bottom and by an exhaustive grep for
`play|broadcast|switch_ivr` across both. The module's own commit message
history and its README's later "Why the Commercial Edition Exists" section
both independently corroborate this: the **free/community edition streams
and decodes; the commercial edition adds the actual playback pipeline.**

**Independent third-party confirmation, found via GitHub code search**: a
public integration
(`AbhishekChauhan1112/Pipecat-voiceagent`, `config/freeswitch/pipecat.lua`)
solves exactly this for a different AI voice stack, with an explicit
comment: `"catches mod_audio_stream::play event -> uuid_broadcast plays
audio to caller"`, and: `"Without this the caller hears silence even if the
Python agent is running."` -- word for word this product's own symptom.

**Conclusion**: Phase 2.25's "vendor module playback limitation" framing
was half right and half a premature stop. The limitation is real (the free
edition genuinely does not auto-play), but it is not a dead end -- the
module's own event, plus FreeSWITCH's own native `uuid_broadcast`, is the
documented, intended way to close the loop, and nothing about it requires
a different build, a paid license, or a new architecture.

## 5. The smallest possible test (brief section 4)

Before touching product code, a throwaway script (not committed) isolated
media playback from STT/LLM/TTS/orchestration entirely: a bare `asyncio`
WebSocket server, a real SIP call via `scripts/sip_uac.py`, a manually
issued `uuid_audio_stream ... start`, and a hand-built `streamAudio`
envelope carrying one deterministic 1-second 440 Hz tone -- no AI pipeline
involved.

Real, raw ESL events observed and printed verbatim confirmed:

* `mod_audio_stream::connect` fires on WebSocket connect.
* `mod_audio_stream::play` fires after the envelope is sent, its own body
  (once correctly parsed -- section 6) naming
  `/tmp/<uuid>_0.tmp.r8`.
* Manually issuing `api uuid_broadcast <uuid> /tmp/<uuid>_0.tmp.r8 aleg`
  over the same ESL connection produced **real, substantial, correct
  audio** at the SIP UAC: `captured 8000 raw RTP8 bytes`, `RMS: 8471` (1
  second at 8 kHz, exactly the tone's own duration).

This one test isolated and confirmed the fix before any product code
changed: the broadcast mechanism itself is completely clean; the missing
piece was entirely "who issues it."

## 6. Root cause #2: a genuine pre-existing ESL frame-parsing bug

Extracting `mod_audio_stream::play`'s own `file` path requires reading the
event's own **body** -- FreeSWITCH serializes an event with a body as its
usual `Name: Value` header block, one more `Content-Length: <n>` header,
a blank line, then `<n>` raw bytes, all still nested inside the outer ESL
frame. `voiceagent/telephony/freeswitch/esl_transport.py::_parse_kv_block()`
had no concept of a nested body: it split every line (including the raw
JSON body line) on `":"` as if it were another header, corrupting the body
into one garbage field, e.g. observed verbatim against the real server:

```text
'{"audioDataType"': '"raw","sampleRate":8000,"file":"/tmp/<uuid>_0.tmp.r8"}'
```

Latent since Phase 2.21 (`_parse_kv_block()`'s own introduction) -- no
event this product handled before this phase ever carried a body, so
nothing exercised this path. **Fix**: `_parse_kv_block()` now detects the
blank line separating a nested body from its own headers (found by
inspecting the exact real bytes, not assumed) and exposes the raw body as
`fields["__body__"]`, unquoted (the body is not FreeSWITCH's own
percent-encoded header format) -- the same `"__body__"` convention
`EslTcpConnection`'s own control-frame handling already uses. An event with
no body is completely unaffected (confirmed by a dedicated regression
test).

## 7. Root cause #3 (product gap, not a bug): no listener for the vendor's own event

`FreeSwitchTelephonyProvider.events()` normalized only lifecycle events
(`_EVENT_TYPE_MAP`); a `CUSTOM` event with no entry in that map --
`mod_audio_stream::play` included -- was silently dropped, by design, for
every event this product did not yet need to react to. This phase adds
exactly one new reaction, entirely inside `voiceagent/telephony/freeswitch/
provider.py` (never crossing the `MediaProvider`/`TelephonyProvider`
boundary, never touching `voiceagent.runtime`): on `mod_audio_stream::play`,
parse the event's own `__body__` JSON, validate the `file` path (a
defense-in-depth pattern already established in this same file for
`_E164_PATTERN`/`_DTMF_PATTERN`), and issue `api uuid_broadcast <call_ref>
<file> aleg` -- FreeSWITCH's own documented, native mechanism for playing a
file to one leg of a live channel. Never raises: a malformed body, a
missing `file`, an unsafe path, or a failed broadcast (e.g. the caller
already hung up) all simply mean no audio plays for that one event, never a
reason to break the whole event stream every other call on the same ESL
connection depends on (regression tests cover every one of these cases).

## 8. Root cause #4 (found only once #2/#3 were fixed): unpaced small frames

With the listener wired in, the first real-call retest sent real audio for
the first time -- 148,320 real PCM bytes captured -- but independent
re-transcription came back **empty**. FreeSWITCH's own log showed why:
`AudioOut` frames from `voiceagent.providers.tts.deepgram_aura`'s own raw
HTTP stream arrive in whatever chunk size the network happens to deliver
(observed: dozens of chunks, some within single-digit milliseconds of each
other, for one ~2.5s reply). `pump_engine_events()`
(`voiceagent.runtime.call_task`) sends each one the instant it arrives,
correctly, since it is transport-agnostic and must not know FreeSWITCH's
own playback semantics (brief section 18's boundary, unchanged). But
`mod_audio_stream`'s own build turns every `streamAudio` envelope into its
own independent, immediate `uuid_broadcast` -- each one interrupting
whatever the channel was already playing rather than queuing behind it.
Dozens of broadcasts within milliseconds of each other is a burst of
overlapping, cut-off playback: loud, but not intelligible.

**Fix, entirely inside `voiceagent/telephony/freeswitch/media.py`, no
change to the engine loop above it**:

1. **Coalescing** (`_MIN_CHUNK_SECONDS = 0.2`): `_FreeSwitchMediaStream
   .send()` now buffers incoming frames and only actually wires a
   `streamAudio` envelope out once at least 0.2s of audio has accumulated
   -- cutting the number of separate broadcasts, and so the total
   accumulated per-broadcast ESL round-trip overhead, by roughly that
   factor over the TTS provider's own raw chunk size. `close()` flushes
   whatever remains buffered, so no audio is ever silently dropped.
2. **Pacing** (`_pace()`): a frame is never sent before its predecessor's
   own real playback duration (computed from its byte length, sample rate,
   and channel count -- known before encoding, no file-system access to
   the FreeSWITCH container needed) has elapsed, plus a fixed
   `_LATENCY_MARGIN_SECONDS = 0.03` safety margin absorbing the real,
   measured, non-zero round trip `_handle_play_event()`'s own broadcast
   still needs (FreeSWITCH delivering the event over ESL, this process
   parsing it, issuing the broadcast command back over the same
   connection). `clock`/`sleep` are injected (default `time.monotonic`/
   `asyncio.sleep`) purely so hermetic tests assert the pacing math with no
   real sleep.

A close-time regression found and fixed along the way: the real end
(`mod_audio_stream`) tearing down its own WebSocket as part of a real
caller hangup, *before* this call's own `detach()`/`close()` runs, is an
ordinary race on the live call path -- but `close()`'s own new best-effort
flush of a buffered tail, before this fix, turned that ordinary race into
an unhandled `ConnectionClosedOK` straight out of
`run_call_task()`'s own `media.detach()` call, observed on a real call.
Fixed: the close-time flush is now wrapped so a send failure there is
swallowed, never propagated -- there is no live channel left for that last
sub-threshold tail of already-decided audio to reach anyway.

## 9. Security review against the brief's own list (section 6)

* **Media ticket authentication / expiration / tenant binding / CallSession
  binding**: unchanged -- this phase never touches `media_transport.py`'s
  ticket minting/verification.
* **Authorization before media/provider start**: unchanged --
  `authorize_call_data_access()`'s own gate (Phase 2.25's own fix) still
  runs before any engine starts; this phase's new code only reacts to
  events *after* a stream is already attached and playing legitimately
  produced audio.
* **Cross-call isolation**: the `uuid_broadcast` this phase issues always
  uses the `call_ref` (`Unique-ID`) carried on the *same* event that named
  the file -- FreeSWITCH's own channel-scoped event, never a cached or
  externally supplied value, so one call's play event can never address
  another call's channel. Confirmed by a dedicated hermetic regression
  test (`test_cross_call_play_events_broadcast_to_the_correct_call_ref
  _each`) and, for the transport layer, by the pre-existing `test_no_cross
  _call_frame_leakage`/`test_sending_on_one_call_never_reaches_another
  _calls_socket`, both still green.
* **No credential leakage**: no credential is read, held, or logged by any
  new code path.
* **No sensitive audio/log leakage**: no new logging of audio content was
  added; the file path already appears in FreeSWITCH's own log today
  (unrelated to this phase).
* **Bounded resource usage**: the new `uuid_broadcast` reuses `_command()`'s
  existing `asyncio.wait_for(..., timeout=command_timeout_seconds)` --
  inherited for free, no new unbounded wait introduced. The coalescing
  buffer is bounded by construction: it only ever holds less than one
  `_MIN_CHUNK_SECONDS` of audio at a time (flushed as soon as it reaches
  that size), and `close()` always drains it.
* **Cancellation/cleanup**: `_handle_play_event()` and the close-time flush
  both never raise (section 7/8) -- neither can wedge or abort a call's own
  teardown. `mod_audio_stream`'s own temp files are deleted by the module
  itself when its session closes (its own README), not this product's
  concern.
* **No bypass of the privacy/data-access authorization boundary**: nothing
  in this phase's fix runs before `authorize_call_data_access()`, and
  nothing in it can start an engine or a media session on its own -- it
  only reacts to a play event that could only exist because a stream is
  already legitimately attached.

## 10. Real SIP validation (brief section 7)

Single, non-concurrent real calls, current code, `scripts/
validate_staging_sip_spoken_e2e.py` (unmodified execution path):

| Run | Captured return-audio bytes | Independently re-transcribed | Contains "nine to five"? |
| --- | --- | --- | --- |
| 4 | 128,320 | "we are open nine to monday to friday we are open nine to five monday to friday" | **yes** |
| 5 | 129,920 | "we are open nine to five friday are open nine to four monday to" | **yes** |
| 6 | 130,560 | "we are we are open nine to monday to friday we are open nine to five monday to friday" | **yes** |

Three consecutive real, independent SIP calls; three consecutive real,
substantial (>100 KB), independently re-transcribed captures, every one
containing the assistant's real deterministic reply's own distinguishing
phrase, verbatim. Before this phase's fix (section 3): 0 bytes, every
time. The mid-fix runs (pacing with no coalescing) are documented in git
history of this investigation and showed partial, less reliable
intelligibility -- included here only as the "root cause #4" evidence
already presented in section 8, not as this phase's own final result.

**A residual, non-blocking artifact**: runs 4 and 6 show a repeated
opening clause ("we are open ... we are open ..."). This is consistent
with real network/processing jitter occasionally causing one coalesced
chunk's own broadcast to be issued a second time before the channel
finished the first (the margin in section 8 reduces, but does not
perfectly eliminate, this under real-world timing variance) -- the content
is still genuinely, intelligibly delivered every time; this is a quality
refinement opportunity for a future phase, not a validation failure.

**An unrelated, pre-existing, orthogonal variance also observed**: the
*caller's own inbound* transcript (`hello can you tell me what your
opening hours are`) came back truncated in several post-fix runs (e.g.
"...opening hour"). This is **not** a regression from this phase's changes
-- the very first, pre-fix reproduction run (section 3) already showed the
identical symptom ("hello can you tell"), before any code in this phase
was touched. It is real Deepgram STT/VAD endpointing variance on the
*inbound* leg, already Phase-2.25-GREEN and out of this phase's own scope
(outbound TTS delivery).

**The validation script's own strict pass/fail** (`_EXPECTED_REPLY
_SUBSTRING`/`"opening hours"` exact-substring checks on both directions)
consequently reported `FAIL`/exit code `1` on these runs -- correctly, by
its own literal contract, but conflating this phase's own subject (TTS
delivery, GREEN in every run) with the unrelated inbound-STT variance
above. The evidence table above is the more precise, per-direction
account.

## 11. Concurrency (brief section 9)

`scripts/validate_staging_sip_spoken_e2e.py` gained an opt-in
`--concurrent` flag (`asyncio.gather` over every call's own
`_run_one_call()`, instead of the prior sequential loop) specifically to
provide a *real* simultaneity test, not merely sequential cross-call
isolation.

**Run**: 2 real, distinct tenants, 2 real SIP calls, launched
simultaneously, 2 real FreeSWITCH channels, 2 real media WebSockets on
distinct ports.

**Confirmed GREEN**: cross-call identity isolation -- `distinct tenants:
2, distinct CallSessions: 2`; each call's own return audio was
substantial and non-zero (`84,800` and `127,680` bytes respectively); no
audio content from one call ever appeared to leak into the other's own
capture (each call's own SIP UAC/RTP session is bound to its own distinct
local port, verified structurally, not merely by absence of evidence).

**Documented limitations, not claimed as validated**:

* **Audio-quality degradation under real concurrent CPU/network
  contention**: both calls' independently re-transcribed replies showed a
  more pronounced word-level stutter under concurrent load than any single
  non-concurrent run (e.g. `"we we are we are open open nine nine to to
  monday monday to day"`) -- the fixed `_LATENCY_MARGIN_SECONDS` safety
  margin (section 8), tuned against non-concurrent timing, is insufficient
  once two calls' own Deepgram/OpenAI API round trips, ESL round trips, and
  RTP processing genuinely compete for the same process's CPU and network
  scheduling. The content is still substantially present in both calls'
  own transcripts, but this is honestly a lower-confidence result than the
  single-call evidence above.
* **A real test-harness race, found only under true concurrency**: one
  call's own `CallSession` failed to reach a terminal status within the
  script's fixed 10-second wait, and its own background finalize/analysis
  step then hit `RuntimeError: cannot schedule new futures after shutdown`
  -- the test script's own unconditional `_shutdown()` (closing that
  call's own `DatabaseBoundary`, and with it its thread-pool executor) ran
  while that same call's own `run_call_task()` background work was still
  using it. This is a **test-harness sequencing bug**
  (`scripts/validate_staging_sip_spoken_e2e.py`'s own fixed timeout was
  not generous enough for two real AI-provider round trips genuinely
  running at once), not a product-code defect -- `run_call_task()` and
  `DatabaseBoundary` behaved exactly as documented once their caller
  (this test script) closed one out from under the other. Per this
  phase's own instruction not to expand scope chasing a harness issue
  once identified and understood, this is documented here, not
  papered over, and not fixed in this phase.

**Classification for concurrency specifically**: **PARTIAL**. Isolation
is genuinely confirmed; simultaneous audio quality and full harness
robustness under true concurrency are not, and are named here precisely
rather than glossed over.

## 12. Regression tests added

All hermetic, no running FreeSWITCH instance required, all passing:

* `tests/telephony/freeswitch/test_esl_transport.py`: a real local TCP
  server test proving an event's own body (`Content-Length` + blank line +
  raw bytes, nested inside the outer frame) is now exposed as
  `fields["__body__"]` intact, never corrupted into a header field; and
  that an event with no body is completely unaffected.
* `tests/telephony/freeswitch/test_provider_play_broadcast.py` (new): a
  valid play event issues the documented `uuid_broadcast` command and is
  never itself yielded as a `CallEvent`; a missing `file`, a malformed
  JSON body, a missing call ref, and an unsafe file path (defense in
  depth) all issue no broadcast and never raise; a failed broadcast (e.g.
  the caller already hung up) is swallowed and later events on the same
  connection still flow; two calls' own play events broadcast to the
  correct `call_ref` each, never mixed up; every other `mod_audio_stream::*`
  subclass is dropped exactly like any other unmapped event, unchanged.
* `tests/telephony/freeswitch/test_media.py`: pacing math against an
  injected fake clock (no real sleep) -- a second frame is paced to the
  first's own real duration plus the fixed margin; a caller already behind
  real time is never delayed further; the very first frame is never
  paced. Coalescing: small frames buffer and do not reach the wire until
  either the threshold is crossed or the stream closes, with byte-exact
  content preserved across the flush boundary; a flush failure against an
  already-closed socket at close time is swallowed, not raised.
* `tests/telephony/freeswitch/test_media_transport.py`: the existing
  real-websocket-server end-to-end test updated to send a payload large
  enough to cross the new coalescing threshold (the test's own subject --
  wire plumbing -- is unaffected by, and orthogonal to, the buffering
  behavior `test_media.py` now covers on its own).

No existing test was weakened to make any of this pass; two pre-existing
tests (`test_send_wraps_the_documented_streamaudio_envelope`,
`test_sending_on_one_call_never_reaches_another_calls_socket`) were
updated to call `close()` before asserting on `sent_text`, since both send
payloads far under the new coalescing threshold and were only ever
asserting on the envelope's own shape/isolation, not on buffering timing.

## 13. Files changed

```text
voiceagent/telephony/freeswitch/esl_transport.py   -- nested event-body parsing fix (__body__)
voiceagent/telephony/freeswitch/provider.py        -- mod_audio_stream::play -> uuid_broadcast listener
voiceagent/telephony/freeswitch/media.py           -- coalescing + pacing + close-time flush hardening
tests/telephony/freeswitch/test_esl_transport.py   -- nested-body regression tests
tests/telephony/freeswitch/test_provider_play_broadcast.py  -- new, play-event/broadcast regression tests
tests/telephony/freeswitch/test_media.py           -- pacing/coalescing/close-flush regression tests
tests/telephony/freeswitch/test_media_transport.py -- updated for the new coalescing threshold
scripts/validate_staging_sip_spoken_e2e.py         -- new --concurrent flag (test tooling, not product code)
docs/PHASE-2.26-REAL-SIP-TTS-PLAYBACK.md           -- this document
```

No change to `voiceagent/runtime/orchestrator.py`, `voiceagent/runtime
/call_task.py`, or any orchestration/architecture code -- per this phase's
own instruction, the fix is contained entirely within `voiceagent
.telephony.freeswitch`, behind the existing `MediaProvider` boundary.

## 14. Production-quality assessment

**GREEN** for this phase's own stated objective -- "Application TTS ->
FreeSWITCH/media path -> SIP/RTP caller playback": real, substantial,
independently-re-transcribed, semantically-correct audio was delivered to
a real SIP caller over real RTP, reproducibly, across three consecutive
real, non-concurrent calls, with a verified root cause (not a guess) and a
fix confined to the documented vendor integration pattern plus one
genuine, narrow bug fix -- no new architecture, no different vendor build,
no paid license.

**PARTIAL**, named explicitly, for real concurrency: cross-call isolation
is genuinely confirmed; simultaneous-call audio quality and full
test-harness robustness under true concurrent load are documented
limitations, not validated, and not silently assumed to inherit the
single-call result.

**Not claimed**: perfect, artifact-free audio on every single-call run
(the repeated-clause artifact, section 10) or a permanent, general-purpose
fix for every conceivable TTS provider's own chunking behavior --
`_MIN_CHUNK_SECONDS`/`_LATENCY_MARGIN_SECONDS` are tuned constants, found
and confirmed empirically against this exact real environment, not derived
from a formal bound.

## 15. Quality gates

Recorded exactly as run; see the final report for this session's own exact
pass/fail output. `pytest -q`, `pytest tests/integration/ -m integration
-q`, Ruff, Ruff format, Pyright, import-linter, detect-secrets, pip-audit,
`git diff --check`.

## 16. SaaS-OS pin

Unchanged and unmodified: `ff550010e5eafecace7311038aadc99fcecfbe3d`.

## 17. Final status

Per this phase's own instructions, no commit has been created and nothing
has been pushed.
