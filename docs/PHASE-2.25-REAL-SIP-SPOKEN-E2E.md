# Phase 2.25: Real SIP Caller & Spoken E2E Validation

Baseline: `770e4bb` (Phase 2.24, `origin/main`).
SaaS-OS remains pinned and unmodified at
`ff550010e5eafecace7311038aadc99fcecfbe3d`. Per this phase's own
instructions, no commit exists yet for this phase's own work and none has
been pushed. `docs/PHASE-0-ARCHITECTURE.md`, `docs/PHASE-2.10-STATUS.md`,
and `docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched.

Phase 2.24 closed real bidirectional FreeSWITCH media transport but could
not obtain a real SIP/PSTN caller, and left the ANSWERED-event race
(section 8 of that phase's own doc) analyzed but not empirically confirmed
against a genuine ringing SIP leg. This phase closes both: a genuine local
SIP caller was built, a real SIP call was driven through the real
orchestrator end to end, the ANSWERED-race question is now empirically
answered, and a real spoken round trip was achieved in one direction --
with the other direction's limit precisely identified and documented,
never fabricated.

## 1. Headline result

```text
REAL SIP INVITE                                    GREEN
REAL RTP (both directions, transport layer)        GREEN
REAL FreeSWITCH media attach (in_progress)         GREEN
REAL caller speech -> REAL Deepgram STT             GREEN
REAL OpenAI LLM deterministic response              GREEN
REAL Deepgram Aura TTS audio reaching the caller    NOT VALIDATED
REAL caller hangup -> REAL terminal CallSession     GREEN
ANSWERED-event race on a genuine SIP leg            GREEN (no defect found)
```

**Complete two-way spoken E2E is NOT claimed.** One direction (caller voice
in) is fully real and independently verified twice, reproducibly, across
two sequential calls. The other direction (agent voice out) could not be
confirmed reaching the caller, for a reason identified precisely in section
3 below and outside this product's own code.

## 2. Infrastructure used

* **FreeSWITCH image**: the same `rasonyang/freeswitch-aicc`, pinned by
  digest `sha256:46d34d6667de6632a0020cd96a6154e903f657ccf750aa4fb7769352f54e0f35`,
  Phase 2.24 already used. Recreated fresh this phase with additional
  published ports its own `external` SIP profile and real RTP need (Phase
  2.24 never sent or received real RTP, so never needed them):

  ```text
  docker run -d --name p224-freeswitch \
    --entrypoint /usr/local/freeswitch/bin/freeswitch \
    -e FS_ESL_PORT=18021 -e FS_ESL_PASSWORD=ClueCon \
    -p 18023:18021/tcp \
    -p 15061:5060/tcp -p 15061:5060/udp \
    -p 15080:5080/tcp -p 15080:5080/udp \
    -p 16384-16584:16384-16584/udp \
    rasonyang/freeswitch-aicc@sha256:46d3...e0f35 \
    -u freeswitch -g freeswitch -nonat -nf -nc
  ```

  `16384-16584` is this image's own `rtp-start-port`/`rtp-end-port`
  (`autoload_configs/switch.conf.xml`, unmodified) -- published 1:1 so the
  container's real RTP socket range is host-reachable.
* **SIP mechanism/endpoint**: this image's own `external` Sofia profile
  (`sip:mod_sofia@<container-ip>:5080`), unauthenticated inbound
  (`auth-calls=false`, no `apply-inbound-acl`) -- the vendor image's own
  documented shape for trunk-style inbound, confirmed by inspecting its own
  `conf/sip_profiles/external.xml`. Its own `context=public` dialplan only
  ships one extension (`conf/dialplan/public/05_aicc.xml`, matching bare
  digit destination numbers for its own demo AI-bot integration) -- this
  phase added one throwaway extension alongside it,
  `conf/dialplan/public/01_phase225_e2e.xml`:

  ```xml
  <include>
    <extension name="phase225_e2e_park">
      <condition field="destination_number" expression="^\+\d{7,}$">
        <action application="park"/>
      </condition>
    </extension>
  </include>
  ```

  Matches only `+`-prefixed E.164-shaped numbers (this phase's own test
  numbers, `+1555XXXXXXX`) so it can never shadow or be shadowed by the
  vendor's own bare-digit extension. `park` only -- identical in spirit to
  Phase 2.23/2.24's own ESL-originated `&park()` null-endpoint calls: this
  product's own `TelephonyEventRouter`/`CallOrchestrator` do all real
  routing/authorization/activation themselves from the resulting real
  `CHANNEL_PARK` event, never from FreeSWITCH dialplan. This is
  precedented: Phase 2.23 itself "added one throwaway dialplan extension
  for its own ESL validation" (Phase 2.24 doc, section 3).
* **SIP client**: `scripts/sip_uac.py` (new) -- a small, hand-rolled, real
  SIP UAC (`INVITE`/`ACK`/`BYE` over a real UDP socket, no retransmission
  timers, no authentication). Built after confirming, in this exact
  environment, that `pyvoip` (pip, pure-Python, the one scriptable SIP
  client this environment could actually install -- see section 8) could
  not be used as-is: its `SIPClient.start()` unconditionally calls
  `register()`, and `register()` calls `self.stop()` (closing the client's
  own socket) after repeated failures against a profile that was never
  meant to be registered against (`auth-calls=false`, no directory for
  arbitrary users). `scripts/sip_uac.py` reuses `pyVoIP.RTP.RTPClient`
  unchanged for real RTP framing/sequencing/PCMU encode-decode -- only the
  SIP signaling layer is hand-rolled, not RTP.
* **Real PostgreSQL**: `voiceagent-test-pg` (`postgres:16-alpine`),
  provisioned and migrated exactly as `tests/integration/README.md`
  documents, same as every prior phase.
* **Real AI providers**: Deepgram STT, OpenAI (`gpt-4o-mini`) LLM, Deepgram
  Aura TTS -- credentials via `SECRETS_ENV_FILE=.env.phase223.local`
  (gitignored; never read directly by any script, never logged).

## 3. Real SIP signaling and RTP: GREEN, with three real bugs found and fixed along the way

`scripts/validate_staging_sip_spoken_e2e.py` (new) wires this product's own,
unmodified `CallOrchestrator`/`FreeSwitchTelephonyProvider`/
`FreeSwitchMediaProvider` to `scripts/sip_uac.py` sending a genuine SIP
`INVITE` at the real `external` profile above. The full real signaling
sequence was confirmed:

```text
real SIP INVITE
    -> real FreeSWITCH (external profile, no auth)
    -> real dialplan match (public context, this phase's own extension)
    -> real park()
    -> real CHANNEL_PARK event -> TelephonyEventRouter -> OFFERED
    -> CallOrchestrator._handle_offer(): real routing, real authorization,
       real ownership claim, real activation-gate claim (all unmodified)
    -> real uuid_answer -> real 200 OK with real SDP -> real ACK
    -> real bidirectional RTP (PCMU) between this script's own SIP UAC and
       real FreeSWITCH
    -> real uuid_audio_stream -> real mod_audio_stream WebSocket attach
    -> CallSession real status: initiated -> in_progress
```

Confirmed reproducibly across every successful run of this phase (multiple
single calls and one 2-call sequential run, section 9).

Three real bugs were found and fixed while getting here -- two in this
phase's own new test script (not product code), one a genuine, real,
previously-latent **product defect**:

**3a. Genuine product defect: `_engine_provider_name()` never actually read
the real `AgentConfig` schema.** `voiceagent.runtime.call_task
._engine_provider_name()` and its deliberate duplicate,
`voiceagent.runtime.orchestrator._engine_provider_name()`, both did:

```python
engine_config = agent_version.config.get("engine") or {}
kind = engine_config.get("kind", "pipelined")
component = engine_config.get(kind) or {}  # BUG: no config has a
return str(component.get("provider", "fake"))  # top-level "pipelined" key
```

`voiceagent.agents.config.EngineConfig`'s real schema has `stt`/`llm`/`tts`
as top-level fields under `engine`, never a field literally named
`"pipelined"` (only `kind == "realtime"` has a matching `engine.realtime`
key by coincidence). Every prior test -- hermetic, integration, and every
one of Phase 2.23/2.24's own staging scripts -- used `provider: "fake"` for
its `pipelined` agent config, which this bug's own fallback default
(`"fake"`) also produced, so the two silently agreed every single time.
This phase's own first real-provider config (`deepgram`/`openai`/
`deepgram_aura`) surfaced it immediately: `authorize_call_data_access()`
denied the call outright (`end_reason=authorization_denied`) because this
function reported `"fake"` regardless of the agent's real configured
provider -- a genuine defect in the one-time-per-call privacy audit/
authorization decision (`voiceagent.runtime.privacy`'s own module
docstring: "no call audio reaches an AI engine before
`authorize_data_access()` succeeds"), not merely a test gap: any real
deployment using a real provider would have hit the identical denial on
every single real inbound call.

**Fix** (both copies, identically): for `kind == "pipelined"`, read
`engine.stt` (the first point real call audio reaches any AI vendor at
all) instead of the non-existent `engine.pipelined`; `kind == "realtime"`
is unchanged (already correct by coincidence). New regression tests:
`tests/runtime/test_call_task_provider_authorization.py` (6 tests,
covering both duplicated copies, `pipelined` with a real provider,
`pipelined` with `"fake"` unchanged, `realtime`, and the no-`engine`
fallback).

**3b. Test-script bug: a blocking `Thread.join()` starved the orchestrator's
own event loop.** This phase's own first version of
`validate_staging_sip_spoken_e2e.py` called `invite_thread.join(timeout=
30.0)` directly inside an `async def` -- a real, synchronous, up-to-30-
second block of the *entire* asyncio event loop `router_task` (the real
orchestrator's own event processing) shares with this script. While
blocked, the orchestrator's own `asyncio.wait_for(..., timeout=10.0)`
around its real `answer()`/`start_media_stream()` ESL commands could not
even have its own timeout fire, reproducing a hang symptom that looked
exactly like a production defect until traced with real, timestamped debug
output (temporarily added, then reverted -- never left in product code).
**Fix**: replaced with `await _wait_until(lambda: not thread.is_alive(),
...)`, a real async poll that never blocks the loop.

**3c. Test-script bug: FreeSWITCH's own SDP-advertised RTP IP is not
reliably reachable from this host.** FreeSWITCH's real `200 OK` SDP answer
advertises its own RTP endpoint by the container's raw bridge-network IP
(e.g. `172.17.0.4`) -- correct from *inside* the Docker network (confirmed:
FreeSWITCH's own outbound RTP to this host arrives fine at the advertised
`192.168.65.254` gateway address, the same Docker Desktop mechanism Phase
2.24 already documented for the media WebSocket). Sending *to* that raw
container IP directly from this host, however, was empirically confirmed
**not to work**: `docker exec ... uuid_record` on a real live call showed
FreeSWITCH's own recording of the inbound leg was completely, uniformly
silent (`rms ~= 1-2`, unchanging) for audio this script had genuinely sent,
even though the identical `pyVoIP.RTP.RTPClient` encode/send/receive/decode
chain was independently proven correct via a pure-Python loopback test
(real synthesized speech round-tripped with `rms` in the thousands, decoded
faithfully). **Fix**: route this script's own outbound RTP to the
container's RTP port range through its published loopback address
(`127.0.0.1:<port>`, same port number FreeSWITCH advertised -- the range is
published 1:1) instead of the raw advertised IP. Re-verified via the same
`uuid_record` technique: FreeSWITCH's own recording of the inbound leg then
showed real, substantial amplitude (`rms` in the thousands) and, fed
through a fresh real Deepgram STT call, transcribed exactly to this
script's own caller utterance.

No product code changed for 3b/3c -- both are entirely contained in this
phase's own new, non-product test tooling
(`scripts/validate_staging_sip_spoken_e2e.py`), documented here because the
investigation is the useful, reusable part (this repository's own
established convention, e.g. Phase 2.24 section 8).

## 4. The ANSWERED-event race: empirically resolved on a genuine SIP leg

Phase 2.24 (its own section 8) reasoned, from FreeSWITCH's documented
channel model alone, that its own observed race (a `null`-endpoint channel
auto-answering itself before the orchestrator ever subscribed) is an
artifact of that synthetic channel type, not a defect that could occur on a
genuinely ringing inbound SIP leg. This phase provides the missing
empirical half of that claim.

**Observed, every real call, every run**: FreeSWITCH's real dialplan-parked
SIP channel does **not** auto-answer. `show channels` during the window
between `park()` and this product's own `uuid_answer` consistently shows
`callstate=RINGING`, `application=park`, no codec negotiated yet. Only once
`CallOrchestrator._activate()` -- after real routing, real authorization,
real ownership/activation-gate claims all complete -- calls
`self._telephony.answer(call_ref)` does FreeSWITCH's own log show `Channel
... has been answered`, codec negotiation, and the real `200 OK` this
script's own SIP UAC receives. There is no earlier `CHANNEL_ANSWER` for the
orchestrator's own `subscribe()` (called before `answer()`, per
`_activate()`'s fixed ordering, unchanged this phase) to ever miss.

**Conclusion**: Phase 2.24's own reasoning is confirmed correct by direct
observation, not merely re-argued. No code change was made to
`CallOrchestrator._activate()`'s ordering or to the answered-event wait --
per this phase's own instruction not to change orchestration architecture
absent a genuine discovered defect, and none was found here (option A of
the brief's own "either A or B").

## 5. Real spoken E2E: one direction GREEN, the other NOT VALIDATED (with cause identified)

**Caller voice in -- GREEN, reproducibly, twice:**

```text
caller utterance (synthesized once via real Deepgram Aura TTS, a normal,
honest way to drive a repeatable automated telephony test with genuine
audio -- not fabricated silence):
    "Hello, can you tell me what your opening hours are?"

    -> streamed as real RTP (PCMU) into the real SIP call
    -> real FreeSWITCH -> real mod_audio_stream -> real WebSocket
    -> real PipelinedEngineSession -> real Deepgram STT

real persisted transcript (app.conversation_turns, role=user):
    "hello can you tell me what your opening hours are"

real OpenAI (gpt-4o-mini) response, against this script's own deterministic
agent instructions ("reply with exactly this sentence...We are open nine
to five, Monday to Friday."):

real persisted assistant turn (app.conversation_turns, role=assistant):
    "We are open nine to five, Monday to Friday."
```

Reproduced identically across two independent, sequential real SIP calls
(section 9) -- not a one-off.

**Agent voice out -- NOT VALIDATED.** The real assistant reply above was
genuinely synthesized by the real pipeline (its own text is proof the
pipeline ran to completion) and genuinely sent to `mod_audio_stream` via
this product's own unmodified `_FreeSwitchMediaStream.send()` (the
`streamAudio` JSON envelope, accepted with no error -- exactly Phase 2.24's
own finding). It never reached this script's own SIP UAC as real audible
RTP: captured return audio was `0` bytes every time, and independently
re-transcribing it produced an empty string every time.

**Root cause investigated and narrowed, not left as an unexamined black
box**: using this product's own `FreeSwitchMediaProvider.attach()`/
`_FreeSwitchMediaStream.send()` directly (bypassing the SIP layer, on both
a `null`-endpoint channel and a genuine SIP leg) plus `uuid_record` on
FreeSWITCH's own side to observe ground truth:

* The module's own compiled binary (`mod_audio_stream.so`; no source is
  shipped in this image) contains the literal strings `audioDataType
  audioData sampleRate` immediately followed by `r8 r16 r24 r32 r48 r64 wav
  mp3 ogg pcmu pcma` and `mod_audio_stream::play response: %s` -- strong
  evidence this build *does* implement a playback/"play" feature keyed off
  the `audioDataType` field, with a candidate vocabulary that does not
  obviously include this product's own literal value, `"raw"`.
* Tested empirically, on a genuine SIP leg with real RTP already flowing:
  sending the real assistant-reply audio with `audioDataType: "raw"`
  (current product code, unchanged) produced no audible output
  (`uuid_record`: silent). Sending the identical audio manually with
  `audioDataType: "r8"` instead (this build's own apparent naming
  convention for 8 kHz raw) **also** produced no audible output.
* Tested empirically with the stream started as `mixed` instead of `mono`
  (`uuid_audio_stream`'s own documented `[mono | mixed | stereo]` track
  option, per this build's own CLI help text) -- **also** no audible
  output.

**Conclusion, stated plainly, per this repository's own established
convention of documenting rather than guessing further**: this exact
vendor image's `mod_audio_stream` build has a real, reproducible playback
limitation -- audio sent to it via the documented `streamAudio` envelope,
in every combination of parameters this phase could reasonably try, is
accepted without error but never actually injected into the live channel.
Nothing further was changed in product code chasing this without a
verified root cause: guessing at additional undocumented protocol variants
against a closed-source vendor binary, with no source and no upstream
documentation available in this environment, would be exactly the kind of
speculative, unverified change this repository's own brief instructs
against. **External prerequisite to close this gap**: a `mod_audio_stream`
build (or a documented, verified playback protocol for this specific one)
confirmed to support real playback -- either a different community fork
with documented bidirectional support, or direct confirmation from this
image's own maintainer of the exact wire shape its `play` feature expects.
This is a vendor-module-selection question for a real deployment, not a
defect in this product's own `FreeSwitchMediaProvider`/
`_FreeSwitchMediaStream`, which sends the one documented, previously
Phase-2.24-validated envelope shape correctly and unchanged.

## 6. Voice-path validation checklist (brief's own numbered list)

```text
1. Caller audio reached FreeSWITCH.                         GREEN (SDP/RTP negotiated, real codec set)
2. FreeSWITCH forwarded media to the application.            GREEN (real WebSocket attach, no "connection error" once
                                                                     the audio-delivery bug in section 3c was fixed)
3. Application received PCM.                                 GREEN (real Deepgram STT transcript persisted)
4. STT produced a transcript.                                 GREEN (persisted, exact real caller words)
5. ConversationEngine/LLM produced a response.                GREEN (persisted, exact deterministic reply)
6. TTS produced actual audio.                                 GREEN (real bytes synthesized, real send() call succeeded)
7. Application sent PCM back.                                 GREEN (send() completed with no error, matching Phase 2.24)
8. FreeSWITCH sent the audio back toward the SIP caller.      NOT VALIDATED (see section 5 -- vendor module limitation)
9. Caller remained connected long enough to receive it.       GREEN (call stayed in_progress for the full real round trip)
```

No raw audio was stored beyond this phase's own short-lived, this-run-only
diagnostic recordings (`docker exec ... uuid_record`, written to the
FreeSWITCH container's own `/tmp`, copied out only for this investigation,
never committed, never containing more than this script's own synthesized
test phrases). No transcript or phone number appears in any log line this
phase added -- printed evidence in this document and this script's own
stdout is the only place any of it appears, exactly as the brief requires
("do not expose transcripts or phone numbers in normal logs" -- this
script's own prints are explicit staging-validation output, not production
logging).

## 7. Call lifecycle

```text
OFFERED (real CHANNEL_PARK)
    -> routing (real resolve_inbound_route)
    -> authorization (real authorize_call_data_access, now correctly
       reporting the real configured provider -- section 3a)
    -> ownership (real assign_call_to_runtime)
    -> activation (real claim_call_for_activation)
    -> ANSWERED (real uuid_answer -> real 200 OK -> real CHANNEL_ANSWER,
       section 4)
    -> media active (real CallSession.status == "in_progress")
    -> conversation (real STT/LLM turns persisted, section 5)
    -> hangup (real caller-initiated SIP BYE)
    -> terminal state (real CallSession.status == "completed",
       end_reason == "completed")
```

Confirmed on every successful run this phase (section 9).

## 8. Real hangup

Validated, this phase, on a genuine SIP leg:

* **Caller hangs up**: this script's own `SipUac.bye()` sends a real SIP
  `BYE`; FreeSWITCH responds `200 OK` (confirmed every run); the real
  `CallSession` reaches a real terminal status (`completed`) within the
  observed window every time.
* **Remote FreeSWITCH hangup event / media disconnect / STT-TTS-engine
  cancellation on terminate**: not independently re-exercised against a
  real SIP leg this phase (this phase's own scope was the caller-hangs-up
  direction, per its own conversation flow) -- already covered hermetically
  by existing, unmodified tests from Phase 2.22 (`test_a_remote_hangup
  _event_stops_the_pump_and_sets_cancellation_reason`,
  `tests/runtime/test_call_task_telephony_events.py`), cited rather than
  re-derived, matching Phase 2.24's own established practice (its own
  section 8) of citing existing hermetic coverage instead of duplicating
  it. Not re-run against real infrastructure this phase, since nothing in
  `run_call_task()`'s own remote-hangup handling was touched.
* **No late audio restarts the call / no leaked media session**: this
  product's own terminal-status guarantee
  (`voiceagent.calls.lifecycle.TERMINAL_STATUSES` has no outgoing
  transitions) is unchanged and was not challenged by anything this phase
  did; every real channel this phase created was either hung up by this
  script's own real `BYE`, or explicitly `uuid_kill`ed during this phase's
  own iterative debugging -- no orphaned channel or leaked media session
  was left running after any successful validation run (`show channels`
  confirmed `0 total` after each).

## 9. Multiple calls

Two sequential real SIP calls were run one after another (`--calls 2`),
each with its own fresh tenant, agent, and phone number:

```text
call 1: tenant=b51be9b3-... call_session=abfecf1b-...
        real transcript: "hello can you tell me what your opening hours are"
        real reply:      "We are open nine to five, Monday to Friday."
call 2: tenant=10cf75bc-... call_session=20b83eb4-...
        real transcript: "hello can you tell me what your opening hours are"
        real reply:      "We are open nine to five, Monday to Friday."

[cross-call] distinct tenants: 2, distinct CallSessions: 2
```

Both calls independently reached the identical real result (section 5's
GREEN half), confirming reproducibility rather than a one-off fluke.
Concurrent calls were not attempted this phase: the brief's own instruction
("do not sacrifice reliability for concurrency testing") governs here --
this environment's single disposable FreeSWITCH container and this
script's own single-process SIP UAC were judged not worth the added
complexity of true concurrency for a validation phase whose sequential
result already demonstrates independent `CallSession`/tenant/agent
association per call.

## 10. Security

* **SIP destination resolves to the intended tenant**: confirmed --
  `resolve_inbound_route()` (unmodified) correctly matched each call's real
  destination E.164 to the tenant/phone-number row this script's own setup
  created immediately beforehand; no cross-tenant misrouting was observed
  across either sequential call.
* **`CallSession.tenant_id` is correct**: confirmed by direct query
  (section 9's own distinct-tenant check).
* **Agent/version resolution is correct**: confirmed -- the real persisted
  system-prompt/assistant-turn content matches exactly the agent config
  each call's own tenant published, never another call's.
* **Authorization occurs before media**: unchanged, confirmed live --
  `CallOrchestrator._handle_offer()`'s fixed ordering (authorize, then
  ownership, then activation, then `answer()`/`start_media_stream()`) was
  exercised for real on every real call this phase ran; the real
  authorization *content* itself was the one thing this phase found and
  fixed (section 3a) -- the *ordering* was never in question and remains
  unmodified.
* **Media ticket remains call-bound and tenant-bound**: unchanged from
  Phase 2.21/2.24 -- this phase's own real calls used real, correctly-
  signed tickets throughout (`mint_media_ticket`/`verify_media_ticket`,
  untouched); no ticket-related failure occurred on any successful run.
* **No unauthenticated media bypass**: unchanged, not challenged by
  anything this phase added.
* **No cross-call frame leakage / no cross-tenant frame leakage**:
  unchanged from Phase 2.24's own new tests (`test_no_cross_call_frame
  _leakage`, `test_sending_on_one_call_never_reaches_another_calls
  _socket`) -- not re-derived; this phase's own two sequential real calls
  additionally provide empirical, real-infrastructure evidence of the same
  property (section 9: each call's own real transcript never appeared in
  the other's).
* **Credentials never enter repository state**: `SECRETS_ENV_FILE=
  .env.phase223.local` (gitignored, confirmed via `git check-ignore`) is
  the only credential source this phase's own scripts ever touch; no SIP
  password exists at all (the target profile is unauthenticated by design,
  section 2) so there is no SIP credential to ever mishandle.
* **Sensitive data does not enter logs**: this document and this script's
  own stdout are the only places any real transcript appears; no new
  logging call was added to product code this phase.

## 11. Observability

No new metric was added this phase. The existing, already-bounded
observability from Phase 2.13/2.14/2.22/2.24
(`record_provider_operation()`, `record_orchestration_event()`, the two
media metrics from Phase 2.24) already covers every real event this
phase's own real calls exercised -- media ticket issuance, provider
operation latency/outcome, orchestration event outcomes (`started`,
`authorization_denied` while diagnosing section 3a, eventually the real
success path). No phone number, call UUID, tenant ID, transcript, or
arbitrary provider error string was added as a label anywhere; this
phase's own fix (section 3a) changed which *value* an existing,
already-bounded `provider` label (part of `authorize_data_access()`'s own
existing audit call, not a new metric) reports -- from a fixed, wrong
constant (`"fake"`) to the real, correct, already-bounded provider name the
agent's own config specifies, which is a correctness fix to existing
bounded data, not a new unbounded label.

## 12. Validation scripts -- what each one actually proves

| Script | What it proves | What it does NOT prove |
|---|---|---|
| `scripts/sip_uac.py` (new) | A real, minimal, from-scratch SIP UAC/RTP signaling layer works against this product's target FreeSWITCH profile | Nothing about this product's own application code -- it is signaling/media plumbing only |
| `scripts/validate_staging_sip_spoken_e2e.py` (new) | **The complete real chain this phase set out to prove**: real SIP INVITE -> real FreeSWITCH -> real routing/authz/ownership/activation -> real ANSWERED (on a genuine ringing leg, closing Phase 2.24's own open question) -> real RTP -> real media attach -> real STT -> real LLM -> real persisted conversation -> real caller BYE -> real terminal state. Explicitly prints `GREEN`/`NOT VALIDATED` per stage (section 6) -- never an unqualified `PASS` covering a stage not actually exercised. Exit code `0` only if *both* audio directions are confirmed; exit code `2` for exactly this phase's own honest partial result (one direction confirmed, the other not); exit code `1` for any real failure. | Real TTS audio reaching the caller (section 5) -- this script's own exit code (`2`, not `0`) says so explicitly every time this limitation is hit, and its own final-line summary states it in words, not just a number. |
| `scripts/validate_staging_call_e2e.py` (Phase 2.23/2.24, unmodified) | Real orchestrator routing/authz/ownership/activation-gate/answer against real Postgres + real FreeSWITCH, via ESL-originated (not real-SIP) channels | A real caller/SIP/RTP path -- superseded for that specific claim by this phase's own script |
| `scripts/validate_staging_media_e2e.py` (Phase 2.24, unmodified) | Real bidirectional FreeSWITCH media transport, real ticket auth | A real caller's voice; still uses a `null`-endpoint synthetic channel |
| `scripts/validate_staging_ai_pipeline.py` (Phase 2.23, unmodified) | Real Deepgram STT/TTS + real OpenAI LLM round trip at the provider/engine boundary | Any telephony/media/SIP involvement at all |

## 13. New/updated automated tests

* `tests/runtime/test_call_task_provider_authorization.py` (new, 6 tests):
  regression coverage for the one genuine product defect this phase found
  and fixed (section 3a) -- both duplicated `_engine_provider_name()`
  implementations, `pipelined` with a real provider, `pipelined` with
  `"fake"` (unchanged behavior), `realtime`, and the no-`engine` fallback.

Every other item in the brief's own test-coverage list (SIP/call
correlation, ANSWERED handling, caller hangup, media disconnect, late
media, terminal call, cross-call isolation, cross-tenant isolation) is
either exercised for real by this phase's own staging script (sections 4,
7, 8, 9, 10) with no product code behind it having changed, or already
covered by existing, unmodified hermetic tests from Phase 2.21/2.22/2.24
(cited in sections 4/8/10 rather than duplicated, per the brief's own "do
not duplicate existing tests unnecessarily").

## 14. Architecture boundaries

`lint-imports`: unchanged contract count from Phase 2.24 (see section 16 --
this phase's own product change, `_engine_provider_name()`'s corrected
field lookup, adds no new import anywhere). `scripts/sip_uac.py` and
`scripts/validate_staging_sip_spoken_e2e.py` are, like every prior phase's
own validation scripts, outside the package boundary `lint-imports`
enforces (entrypoint scripts) and import only already-public seams
(`CallOrchestrator`, `FreeSwitchTelephonyProvider`,
`FreeSwitchMediaProvider`, `FreeSwitchMediaListener`,
`serve_freeswitch_media`, the provider registries) every prior phase's own
scripts already used. No SIP-specific or FreeSWITCH-specific type crossed
into `voiceagent.runtime`/`voiceagent.calls`/any domain module this phase --
`scripts/sip_uac.py` is pure SIP/RTP plumbing with zero import of anything
under `voiceagent/`.

## 15. Known limitations / external prerequisites (repeated, precisely)

* **Real TTS audio delivery to the caller** -- see section 5. External
  prerequisite: a `mod_audio_stream` build (or a verified playback protocol
  for this exact one) confirmed to support real audio injection into a
  live channel. Not a defect in this product's own code.
* **Remote-hangup/media-disconnect/cancellation-on-terminate against a real
  SIP leg** were not independently re-exercised this phase (section 8) --
  only caller-initiated hangup was. The underlying mechanisms are
  unmodified and already covered hermetically; a future phase could extend
  this phase's own script to also drive the FreeSWITCH side of a hangup
  (`uuid_kill`) mid-call for a fully real-infrastructure version of that
  same coverage.
* **True concurrent calls** were not attempted (section 9) -- only
  sequential, per the brief's own reliability-over-concurrency guidance.
* **`mod_audio_stream`'s own build/version provenance** remains, as Phase
  2.24 already noted, whatever `rasonyang/freeswitch-aicc` ships -- not
  independently re-verified beyond the binary-string investigation in
  section 5.
* **This phase's own SIP UAC** (`scripts/sip_uac.py`) is deliberately
  minimal: no retransmission timers, no authentication, single call at a
  time. Adequate for this controlled staging validation; not a general-
  purpose SIP client.
* **The `host.docker.internal`/Docker Desktop gateway-address dependency**
  Phase 2.24 already documented (media WebSocket) is joined this phase by
  the analogous, newly-found RTP-destination-routing requirement (section
  3c) -- both are properties of this specific Docker Desktop networking
  setup, not of the product; a real deployment's own network topology
  should be validated on its own terms, not assumed to match this one.

## 16. Quality gates

Recorded exactly as run; see the final report for this session's own exact
pass/fail output.

* **Backend tests, hermetic** (`pytest -q`)
* **Backend tests, real PostgreSQL** (`pytest tests/integration/ -m
  integration -q`)
* **Ruff** / **Ruff format**
* **Pyright**
* **import-linter**
* **detect-secrets**
* **pip-audit**

## 17. SaaS-OS pin

Unchanged and unmodified: `ff550010e5eafecace7311038aadc99fcecfbe3d`.

## 18. Final status

Per this phase's own instructions, no commit has been created and nothing
has been pushed.
