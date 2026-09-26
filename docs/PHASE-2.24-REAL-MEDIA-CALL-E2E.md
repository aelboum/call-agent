# Phase 2.24: Real Media & Call E2E Validation

Checkpoint: `28034b1 feat: validate real staging call e2e` (Phase 2.23) is
the branch's tip commit at the start of this phase. SaaS-OS remains pinned
and unmodified at `ff550010e5eafecace7311038aadc99fcecfbe3d`. No commit
exists yet for this phase's own work; per its own instructions, this phase
implements and validates only -- it does not commit or push.
`docs/PHASE-0-ARCHITECTURE.md`, `docs/PHASE-2.10-STATUS.md`, and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched.

Phase 2.23 closed real FreeSWITCH ESL, real orchestration, and real
STT/LLM/TTS provider validation, but left bidirectional FreeSWITCH↔
application media and any real caller path genuinely unvalidated (no
`mod_audio_stream` in that phase's own image, no SIP endpoint). This phase
closes the media half for real, found and reasoned through a real
answered-event race in the process, and documents -- without fabricating
success -- exactly why the caller-path half still could not be closed here.

## 1. Infrastructure used

* **FreeSWITCH image**: `rasonyang/freeswitch-aicc:latest`, pinned by digest
  `sha256:46d34d6667de6632a0020cd96a6154e903f657ccf750aa4fb7769352f54e0f35`.
  Chosen after confirming (`docker search freeswitch`) it is one of the few
  publicly available images that ships `mod_audio_stream` pre-built --
  Phase 2.23's own `safarov/freeswitch` does not, and building FreeSWITCH
  with this third-party module from source was judged out of proportion for
  a validation phase (brief's own "do not add speculative complexity").
  Its own entrypoint (`docker-entrypoint.sh`) refuses to start without a
  PostgreSQL DSN for its call-center-specific directory/dialplan (an
  unrelated feature of that image, not needed here) -- run instead with
  `--entrypoint /usr/local/freeswitch/bin/freeswitch -u freeswitch -g
  freeswitch -nonat -nf -nc`, bypassing only that gate. Its own
  `event_socket.conf.xml`/`acl.conf.xml` already bind on `0.0.0.0` and
  permit RFC1918 ranges by default -- no config edits were needed for ESL
  reachability this time (unlike Phase 2.23's own image).
* **Media module**: `mod_audio_stream.so`, confirmed loaded
  (`api module_exists mod_audio_stream` → `true`) and its `uuid_audio_stream`
  command confirmed present and functional (section 2). Version/build
  provenance beyond "whatever this image ships" was not independently
  re-verified -- documented as a limitation (section 14).
* **Real PostgreSQL**: the same `voiceagent-test-pg` container Phase
  2.22/2.23 already used.
* **Real AI providers**: unchanged from Phase 2.23 -- Deepgram STT,
  Deepgram Aura TTS, OpenAI LLM, all already validated for real; not
  re-exercised against a live call this phase (see section 9).
* **Real caller mechanism**: none obtained -- see section 3.

## 2. Real FreeSWITCH media module: `uuid_audio_stream` is genuinely functional

`scripts/validate_staging_media_e2e.py` (new) connects this product's own,
unmodified `FreeSwitchTelephonyProvider`/`FreeSwitchMediaProvider`/
`FreeSwitchMediaListener` to the real image above and validates, all for
real:

1. A real `wss://` listener (`serve_freeswitch_media()`) is reachable from
   inside the FreeSWITCH container.
2. `api uuid_audio_stream <uuid> start <url> mono 8k` against the live
   server makes real FreeSWITCH open a real WebSocket connection to it.
3. The connection presents a real signed ticket
   (`mint_media_ticket()`/`verify_media_ticket()`), verified before
   `register_socket()` is ever called.
4. Real raw binary PCM frames arrive -- **320 bytes per frame**, exactly
   160 16-bit samples at 8 kHz, exactly this product's own documented
   `mod_audio_stream` frame-size assumption (`voiceagent.telephony
   .freeswitch.media`'s own docstring), confirmed against a real server for
   the first time.
5. A real JSON `streamAudio` envelope sent back is accepted by the live
   server with no error.

Run 3 times in a row with no flakiness.

**A real, generalizable finding**: this build's own websocket client
(`audio_streamer_glue.cpp`) failed every connection attempt instantly
(`connection error`, no real TCP attempt visible in FreeSWITCH's own debug
log) when given `host.docker.internal` as the media URL's hostname -- it
does not resolve hostnames the way a normal client does. Using the
container's actual reachable IP (Docker Desktop's own gateway,
`192.168.65.254` here; `getent hosts host.docker.internal` inside the
container prints it) works correctly. Documented in
`scripts/validate_staging_media_e2e.py`'s own docstring and its
`--media-public-base-url` argument's help text; no product code was changed
for this since it is a deployment-time configuration fact (what value
`VOICEAGENT_FREESWITCH_MEDIA_PUBLIC_URL` must hold for a given FreeSWITCH
deployment), not a defect in `FreeSwitchTelephonyProvider` itself.

**The existing signed-ticket authorization was not weakened in any way** --
the real connection above only succeeded because it presented a real,
correctly-signed ticket for the exact `call_ref` in play; a second manual
attempt with a garbage ticket string (`/media/test`) was correctly rejected
before `register_socket()` (confirmed via the FreeSWITCH-side log showing
the connection closing immediately, and independently by the existing
`test_end_to_end_rejects_an_invalid_ticket_and_closes_the_connection` test).

## 3. Real caller path: not achieved -- exact prerequisite documented

Investigated, in the brief's own stated order of preference:

* **SIP softphone**: no scriptable/CLI SIP client is installed or readily
  installable in this environment -- checked for `pjsua2` (pip has no
  prebuilt wheel; it requires a compiled PJSIP build), `sipsimple` (no
  distribution found), `baresip`/`linphone-cli`/`sipexer`/`sipp` (none on
  `PATH`, none packaged for quick install here). `winget search sipp` found
  only `miniSIPPhone`, a GUI-only softphone with no scriptable/CLI
  interface -- unusable without a human operating it, which this session
  cannot provide.
* **Controlled SIP endpoint / test SIP client**: the FreeSWITCH image's own
  internal SIP profile has a directory driven by its own PostgreSQL/Lua
  integration (`aicc_xml.lua`), not a plain XML directory this phase could
  add one throwaway user to the way Phase 2.23 added one throwaway dialplan
  extension for its own ESL validation.
* **PSTN/SIP trunk**: none configured or available; the brief's own
  instruction is explicit not to introduce a commercial provider dependency
  merely for testing, and not to invent/mock this path.

**Conclusion, stated plainly, per the brief's own instruction not to invent
or mock this**: a real caller path requires either a human operating a real
or virtual SIP phone, or hand-building a raw SIP user-agent client from
scratch (a substantial, error-prone undertaking -- SDP negotiation, RTP
framing, full SIP transaction handling -- disproportionate to what this
validation phase should be building, and itself "speculative complexity"
the brief asks this phase to avoid). **External prerequisite**: a SIP
softphone (or scriptable SIP UAC) a human can operate, or SIP/PSTN trunk
credentials, whichever is more practical for the deploying team to obtain.

## 4. Real spoken E2E: not achieved, for the reason in section 3

No caller audio ever reached the application this phase, so no real spoken
round trip over a live call was performed. Section 6's real-vs-hermetic
table states this precisely. Real STT/LLM/TTS were already independently
proven for real in Phase 2.23 and were not re-run against a live call this
phase (no live call existed to run them against).

## 5. Real media attachment sequence -- validated

Section 2's script validates the complete sequence the brief names:

```text
channel (real FreeSWITCH null endpoint)
    -> FreeSWITCH (real uuid_audio_stream)
    -> application media listener (real wss:// server, this host)
    -> authenticated media attachment (real signed ticket verified)
    -> inbound PCM frames (real, 320 bytes/frame)
    -> application audio processing (FreeSwitchMediaProvider.attach()/receive())
    -> outbound PCM frames (real streamAudio envelope, accepted)
    -> FreeSWITCH
    -> channel
```

Preserved, unchanged, confirmed still true:

* **Bounded queues/backpressure**: `PipelinedEngineSession`'s own
  `audio_queue_maxsize`/`event_queue_maxsize` (real `asyncio.Queue(maxsize=
  ...)`, already built and tested -- `tests/providers
  /test_pipelined_engine_hardening.py`) were not touched. The media
  transport layer itself (`WebSocketMediaSocket`) has no explicit queue of
  its own by design: `receive_binary()` pulls directly from the real
  `websockets` connection's own `recv()`, so a slow consumer naturally
  backpressures the sender through ordinary TCP flow control -- there is no
  unbounded buffer anywhere in this path to bound.
* **Closed-session protection**: `_FreeSwitchMediaStream.send()` already
  raises on a closed stream (unchanged); confirmed still covered by
  `tests/telephony/freeswitch/test_media.py`.
* **Tenant isolation**: this layer has no concept of tenant at all, by
  design -- see section 12.
* **Signed media tickets / authorization before activation**: unchanged,
  confirmed live in section 2.
* **Runtime ownership semantics**: unchanged; not modified.
* **No media path direct database access**: confirmed by inspection --
  `media.py`/`media_transport.py` import nothing from `voiceagent.db`/
  `infra.db`.
* **No provider-specific types leaking into domain/application layers**:
  confirmed by inspection and by `lint-imports` (section 13) -- `mod_audio
  _stream`'s own envelope shape is constructed/parsed entirely inside
  `voiceagent.telephony.freeswitch.media`; `voiceagent.telephony.contracts
  .MediaStream`/`AudioFormat` (opaque `bytes`) are the only types anything
  above this package ever sees.

## 6. Real-vs-hermetic summary

```text
real media transport (attach/PCM in/streamAudio out) validated   YES  (section 2)
real caller/SIP/PSTN path validated                               NO   (section 3)
real spoken conversation over a live call validated                NO   (section 4)
```

This is a strictly more complete "real" picture than Phase 2.23's own
(which could not even attempt real media), but it does not, and must not
be read to, claim a complete spoken inbound call.

## 7. Call lifecycle / `MEDIA_ACTIVE`

**No new `CallSession` lifecycle state was introduced.** `MEDIA_ACTIVE`, as
named in the brief, is not a state this product's authoritative lifecycle
(`initiated, ringing, answered, in_progress, completed, failed,
interrupted`) needs to represent: media attachment is a real operational
condition entirely internal to `CallOrchestrator._activate()`/
`run_call_task()` (whether `FreeSwitchMediaProvider.health(call_ref)
.attached` is true), never something anything outside those two functions
queries or branches on. Introducing a new authoritative state for it would
duplicate information already fully recoverable from existing operational
signals (the media metrics added in section 9, `StreamHealth`) without
adding any real domain capability -- exactly the "do not introduce a new
lifecycle state unless the existing model genuinely requires it" case the
brief anticipates. No change was made here.

**Terminal calls cannot be restarted through late media/provider events**:
unchanged from Phase 2.22's own `claim_call_for_activation()`/`is_valid
_transition()` guarantees (a terminal status has no outgoing edges,
`lifecycle.py`); not re-derived or modified this phase, and nothing in this
phase's own real validation ever attempted to restart a terminal call.

## 8. The ANSWERED-event race: a real finding, analyzed and *not* a genuine defect

Revisiting Phase 2.21/2.23's own documented uncertainty with real media
now available surfaced a real, reproducible race -- worth recording in
detail because the investigation, not just the conclusion, is the useful
part.

**What was observed**: running `scripts/validate_staging_call_e2e.py`
(Phase 2.23's own real-orchestrator script, extended this phase with a real
media listener -- section 2) against the real `mod_audio_stream` image, the
real `CallSession` reached `failed`/`media_unavailable` even though
FreeSWITCH's own log showed `uuid_audio_stream` genuinely succeeding
(`Setting BUG Codec L16:100`) -- the orchestrator itself hung up the
channel 5 seconds later (`answer_timeout_seconds`), i.e. it timed out
waiting for an `ANSWERED` event that, per the log, never arrived after that
point.

**Root cause**: the test channel is FreeSWITCH's own `null` endpoint (no
real SIP signaling), which auto-answers itself as part of channel creation
-- its own `CHANNEL_ANSWER` fires within milliseconds, *before* the
orchestrator's own routing/authorization/ownership-claim database
round-trips finish and `CallOrchestrator._activate()` ever calls
`self._telephony_events.subscribe(call_ref)`. `TelephonyEventRouter`'s own
documented behavior -- an event for a `call_ref` with no current subscriber
is silently dropped -- correctly drops that real `ANSWERED` event as
unroutable. The orchestrator's own subsequent `answer()` call (an
idempotent re-answer of an already-answered channel) succeeds but produces
no *second* `CHANNEL_ANSWER` event, so `_wait_for_answered()` waits for one
that will never come.

**Why this is not a genuine production defect**: a real inbound SIP/PSTN
call never auto-answers itself -- FreeSWITCH parks a genuine inbound leg in
an unanswered/ringing state (`park()`, `CHANNEL_PARK`/`OFFERED`) and it
stays there until something explicitly calls `uuid_answer`. For that real
call shape, `CallOrchestrator._activate()`'s own ordering -- subscribe,
*then* call `answer()`, *then* wait for the `ANSWERED` event that call
itself causes -- is correct and sufficient by construction: there is no
event to miss, because nothing answers the channel before the
orchestrator's own explicit command does. The race observed here is
specific to the `null` endpoint's own auto-answer behavior, which exists
only as an artifact of not having a real SIP signaling path (section 3) to
test against instead.

**Verified as part of this same investigation** (all per the brief's own
list, either directly observed or already covered by existing Phase 2.22
tests, cited rather than re-proven where already tested):

* Media attachment did *not* race ahead of authorization or ownership --
  confirmed by the real log: authorization/ownership/`answer()` all
  completed, in order, before `start_media_stream()` ran, exactly matching
  `CallOrchestrator._handle_offer()`'s own fixed sequence (unchanged this
  phase).
* Duplicate `ANSWERED` events are harmless -- `voiceagent.calls.lifecycle
  .is_valid_transition()`'s own same-state-is-always-valid rule, unchanged;
  not re-derived here.
* Delayed `ANSWERED` events cannot activate an already-terminal call --
  `TelephonyEventRouter` only delivers to a *subscribed* `call_ref`, and
  `voiceagent.runtime.call_task` unsubscribes on completion; a delayed
  event for a call whose task has already ended is dropped the same way an
  unrouted one is.
* Remote hangup during activation -- `test_a_remote_hangup_event_stops
  _the_pump_and_sets_cancellation_reason` (`tests/runtime
  /test_call_task_telephony_events.py`) already covers this hermetically;
  not re-derived here.

**No code change was made** for this finding -- per the brief's own
instruction ("fix only genuine defects found during this phase"), and
because the analysis above concludes there is no defect in the real target
scenario (a genuinely-ringing inbound call) to fix. Documented here as the
substantive result of revisiting section 7's own question, not left silent
merely because the answer turned out to be "no code change needed."

## 9. Reconnect / parked-channel reaper -- remains correctly deferred

Phase 2.21's own documented deferral (`docs/PHASE-2.21
-FREESWITCH-TELEPHONY-INTEGRATION.md`, "Parked-channel reaper / two-layer
concurrency reconciliation deferred") is unchanged this phase. Evaluated
against the brief's own test -- "does production safety require this
before real staging E2E can be trusted" -- the answer remains no, for the
same reason Phase 2.21 gave plus one this phase's own validation runs
themselves demonstrate: every real channel this phase created was either
hung up by the orchestrator's own bounded failure path, explicitly killed
via `uuid_kill` at the end of a validation script, or torn down when its
container was removed -- no long-lived orphaned channel was ever left
behind, because these are short, bounded, single-call validation runs, not
a long-running production deployment under real load. Building a new
periodic reaper (a background job scanning FreeSWITCH's own channel list
against known `CallSession` ownership) is a substantial new feature, not a
validation-phase fix, and the brief's own "do not add speculative
complexity" instruction governs here. Left deferred, unchanged.

## 10. Observability

Two new, bounded metrics added (`voiceagent/metrics.py`), matching the
brief's own list and its own "never unbounded labels" rule exactly:

* `voiceagent.media.tickets_rejected` (counter, labeled only by
  `reason` ∈ `{malformed, expired, invalid_signature, missing_path}` --
  `TicketVerificationError`'s own bounded vocabulary, never the ticket, the
  path, or a header value) -- wired into `FreeSwitchMediaListener
  .handle_connection()`'s own rejection path.
* `voiceagent.media.session_duration` (histogram, no labels) -- wired into
  `FreeSwitchMediaProvider.detach()`, measuring real attach-to-detach
  wall-clock time.

Everything else the brief asks for already existed, unchanged this phase,
and is cited rather than duplicated: "media connection accepted/rejected"
and "media attach latency" -- `voiceagent.provider.operations`/
`operation_latency` via `record_provider_operation("media", "attach"/
"detach", ...)` (Phase 2.13/2.14). "STT/TTS failure" -- the same
`record_provider_operation()` family, called from `PipelinedEngine`'s own
STT/LLM/TTS call sites (Phase 2.3/2.20). "Real-call E2E outcome" --
`voiceagent.orchestration.events` (`record_orchestration_event()`, Phase
2.22), whose bounded outcome vocabulary already includes exactly this.

No per-audio-frame span was added anywhere; no unbounded label (call UUID,
tenant ID, transcript, phone number, arbitrary provider error text) is
attached to any metric old or new.

## 11. Validation scripts -- what each one actually proves

| Script | What it proves | What it does NOT prove |
|---|---|---|
| `scripts/validate_staging_freeswitch.py` (Phase 2.23) | Real FreeSWITCH ESL protocol (auth, commands, events) | Media, AI, or caller path |
| `scripts/validate_staging_media_e2e.py` (new) | **Real bidirectional media transport**, real ticket auth, real PCM both directions | A real caller's voice; uses a `null`-endpoint synthetic channel |
| `scripts/validate_staging_call_e2e.py` (Phase 2.23, extended this phase with a real media listener + `--media-public-base-url`) | Real orchestrator routing/authz/ownership/activation-gate/answer against real Postgres + real FreeSWITCH, with real media now wired in | A complete spoken call -- ends at the answered-event race (section 8), by design of the `null`-endpoint test channel, not a real ringing call |
| `scripts/validate_staging_ai_pipeline.py` (Phase 2.23) | Real Deepgram STT/TTS + real OpenAI LLM round trip | Any telephony/media involvement at all |

No script in this set ever prints `PASS` for a stage it did not actually
exercise -- each one's own `FAIL:` branches name the exact boundary that
failed, and `scripts/validate_staging_call_e2e.py` explicitly treats a
still-real, still-meaningful outcome (`media_unavailable`, or now the
answered-event race) as its own documented, correctly-labeled result rather
than silently converting it into an unqualified `PASS`.

## 12. Security review

* **Media ticket authenticity**: HMAC-SHA256 over `(call_ref, expiry)`,
  `hmac.compare_digest` (constant-time) -- unchanged, confirmed live
  (section 2).
* **Ticket expiration**: enforced (`verify_media_ticket`'s own `time.time()
  > expiry` check) -- unchanged; a fresh 60s-TTL ticket was used in every
  real test this phase, well within bounds.
* **Tenant binding**: the ticket carries `call_ref` only, never a tenant id
  -- correct by design (`docs/PHASE-0-ARCHITECTURE.md` §14.3, quoted in
  `media_transport.py`'s own docstring): tenant binding happens one layer
  up, when `call_ref` is first associated with a `CallSession` (which does
  carry `tenant_id`, RLS-protected). A ticket cannot be presented for a
  `call_ref` it was not minted for (signature covers `call_ref` itself).
* **Call-session binding**: `register_socket()`/`attach()` are both keyed
  strictly by `call_ref`; confirmed no cross-call frame delivery is
  possible even at the socket-registration layer (new tests, section 5 of
  the brief / this doc's own testing section below).
* **Authorization-before-media**: unchanged, confirmed live -- media attach
  is never reached before `authorize_call_data_access()` succeeds
  (`CallOrchestrator._handle_offer()`'s own fixed ordering).
* **No media listener bypass**: `handle_connection()`'s ticket check runs
  before `register_socket()` on every path, with no alternate entry point;
  confirmed by inspection and by the new metric now recording every
  rejection.
* **No direct unauthenticated WebSocket access**: `extract_call_ref()`
  rejects any path without `/media/<ticket>`; confirmed by
  `test_extract_call_ref_rejects_a_path_with_no_ticket_segment` (existing).
* **No cross-tenant frame routing**: this layer has no tenant concept to
  route by (see above) -- the only thing that could leak is cross-*call*
  routing, closed by the new `test_no_cross_call_frame_leakage`/
  `test_sending_on_one_call_never_reaches_another_calls_socket` tests
  (section 13).
* **No sensitive data in logs**: this phase's own new log point
  (`media.ticket_rejected`, pre-existing) carries no ticket/path/header
  value; the new metric carries only the bounded `reason` enum. No raw
  audio or transcript was logged by anything touched this phase.
* **Bounded memory**: no new unbounded buffer was introduced (section 5);
  the media transport layer's own lack of an internal queue is itself the
  bound (backpressure via the underlying real TCP/WebSocket connection).
* **Bounded network behavior**: no new unbounded retry/reconnect loop was
  introduced; `ManagedEslConnection`'s own existing bounded backoff is
  unchanged.
* **Cancellation**: `_FreeSwitchMediaStream.close()`/`detach()` unchanged;
  confirmed still correctly bounded by the existing `media_detach_timeout
  _seconds` machinery (`run_call_task()`, unchanged, tested in
  `test_run_call_task_teardown_is_bounded_by_a_wedged_media_detach`).
* **Shutdown**: `FreeSwitchMediaListener`'s own server `close()`/
  `wait_closed()` is unchanged; `scripts/validate_staging_media_e2e.py`'s
  own teardown exercises exactly this path for a real server with an
  active connection.
* **Replay resistance**: a ticket's signature covers `expiry`, so a
  captured, expired ticket cannot be replayed after its TTL; a captured,
  *unexpired* ticket could in principle be reused for the *same* `call_ref`
  before it expires -- unchanged from Phase 2.21's own original design
  (single-purpose, short-lived, not single-use) and not a regression
  introduced this phase; noted as an existing, known, bounded-risk property
  (a 60-second window, one specific `call_ref`), not a new finding.

## 13. New/updated automated tests

* `tests/telephony/freeswitch/test_media.py`:
  `test_no_cross_call_frame_leakage`,
  `test_sending_on_one_call_never_reaches_another_calls_socket` (new).
* `tests/telephony/freeswitch/test_media_transport.py`:
  `test_end_to_end_client_disconnect_during_active_media_ends_the_stream
  _cleanly` (new, real local `websockets` client/server, not a mock).

Every other item in the brief's own section-5 list was found already
covered by existing tests from earlier phases and cited rather than
duplicated (see section 5/8/9 above for the exact citations): invalid/
expired/tampered ticket, unknown call session, duplicate attachment,
bounded queue/backpressure, caller hangup during media, TTS/STT
cancellation on terminate, media-detach-bounded teardown, runtime shutdown
with active calls, no cross-call leakage at the call_task/runtime level (10
simultaneous calls).

## 14. Architecture boundaries

`lint-imports`: 8/8 contracts kept, 0 broken (unchanged count from Phase
2.23 -- the two new metric functions and the `TicketVerificationError
.reason` attribute added no new cross-package import; `voiceagent.metrics`
was already importable from `voiceagent.telephony.freeswitch.media_transport`'s
sibling module `media.py`, and remains a leaf `infra`-adjacent module with
no product-layer dependency of its own).

No FreeSWITCH-specific type crossed into `voiceagent.runtime`/
`voiceagent.calls`/any domain module this phase -- the two new scripts
(`scripts/validate_staging_media_e2e.py`, and the extension to
`validate_staging_call_e2e.py`) are themselves outside the package boundary
`lint-imports` enforces (entrypoint scripts), and import only the same
already-public seams (`FreeSwitchTelephonyProvider`, `FreeSwitchMediaProvider`,
`FreeSwitchMediaListener`, `serve_freeswitch_media`) every prior phase's own
scripts already used.

## 15. Known limitations / external prerequisites (repeated, precisely)

* **No real SIP/PSTN caller path** -- see section 3. External prerequisite:
  a SIP softphone/scriptable UAC a human can operate, or SIP/PSTN trunk
  credentials.
* **No complete real spoken conversation** -- direct consequence of the
  above; not fabricated as a PASS anywhere in this phase's own scripts or
  this document.
* **`mod_audio_stream`'s own version/build provenance** was not
  independently re-verified beyond "whatever `rasonyang/freeswitch-aicc`
  ships" -- a production deployment should pin and audit its own chosen
  build, not necessarily this exact community image.
* **The `host.docker.internal` hostname-resolution failure** (section 2) is
  specific to this module's own websocket client and this exact Docker
  Desktop setup -- a real deployment's own `VOICEAGENT_FREESWITCH_MEDIA
  _PUBLIC_URL` should be validated against its own real FreeSWITCH/network
  topology rather than assumed to work with any hostname.
* **Parked-channel reaper** remains deferred (section 9), unchanged from
  Phase 2.21's own decision.
* **The ANSWERED-event race** (section 8) was reproduced only against a
  `null`-endpoint synthetic channel; it was not, and per section 3 could
  not be, confirmed absent against a genuine ringing SIP leg -- the
  analysis in section 8 is reasoning from FreeSWITCH's own documented
  channel model, not an empirical real-SIP-call observation.

## 16. Quality gates

* **Backend tests, hermetic** (`pytest -q`): full suite passes, including
  the 3 new tests (section 13).
* **Backend tests, real PostgreSQL** (`pytest tests/integration/ -m
  integration -q`): full suite passes.
* **Ruff** / **Ruff format**: all checks pass.
* **Pyright** (`voiceagent`, `tests`, `scripts`): 0 errors.
* **import-linter**: 8 contracts kept, 0 broken.
* **detect-secrets**: one genuine new finding in this phase's own work
  (`scripts/validate_staging_call_e2e.py`'s disposable, this-run-only
  ticket-secret literal) -- annotated `# pragma: allowlist secret` matching
  this repository's own established convention for exactly this kind of
  disposable local default (`docker-compose.yml`'s own `devpassword`
  entries); re-scanned clean afterward. Every other file the fresh scan
  touched is pre-existing, unrelated drift (confirmed by diffing the
  scan's file-set against the baseline), not this phase's own work; the
  baseline itself was left untouched.
* **pip-audit**: no known vulnerabilities.
* Frontend gates: not applicable -- no frontend file was touched.

## 17. SaaS-OS pin

Unchanged and unmodified: `ff550010e5eafecace7311038aadc99fcecfbe3d`.

## 18. Final status

Per this phase's own instructions, no commit has been created and nothing
has been pushed.
