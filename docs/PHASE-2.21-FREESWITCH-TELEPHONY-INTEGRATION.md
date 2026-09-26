# Phase 2.21: Real FreeSWITCH Telephony & Media Integration

Checkpoint: `4d480ef feat: add OpenAI staging provider integration` (Phase
2.20) is the branch's tip commit at the start of this phase. SaaS-OS
remains pinned and unmodified at `ff550010e5eafecace7311038aadc99fcecfbe3d`.
No commit exists yet for this phase's own work; per its own instructions,
this phase implements and reports only -- it does not commit or push.
`docs/PHASE-0-ARCHITECTURE.md` and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched.

This phase builds the first real transport underneath the existing
`TelephonyProvider`/`MediaProvider` contracts: a real TCP ESL client, a real
`wss://` media listener, and the demultiplexing layer that lets FreeSWITCH's
own lifecycle events reach the existing call state machine -- without
redesigning `ConversationEngine`, `CallRuntime`, or any AI-provider
abstraction.

## 1. Repository baseline (inspected before any change)

- **`voiceagent/telephony/contracts.py`**: `TelephonyProvider`/`MediaProvider`
  Protocols, `CallEvent`/`CallEventType`/`HangupCause`/`AudioFormat`, all
  already vendor-neutral and sufficient -- not modified. `CallRef` is a
  plain `str` with no built-in structure; correlation is entirely the
  adapter's responsibility.
- **`voiceagent/telephony/freeswitch/esl.py`**: `EslConnection` was a
  `Protocol` only (`send`, `events`) -- explicitly, by its own docstring, no
  real TCP transport existed anywhere in this repository before this phase.
- **`voiceagent/telephony/freeswitch/provider.py`**: `FreeSwitchTelephonyProvider`
  already correctly bounds every ESL command (`_command()`,
  `asyncio.wait_for`) and normalizes events -- real, tested logic over an
  injected (until now, only fake) transport. **A concrete defect found and
  fixed**: `originate()`/`transfer()` treated `bgapi`'s immediate reply (a
  Job-UUID) as the new channel's own `CallRef` -- never verified against
  real ESL semantics, and inconsistent with `docs/PHASE-0-ARCHITECTURE.md`
  §10.5's own documented design (a product-minted `origination_uuid`
  channel variable). Fixed as the smallest necessary correction (section
  16): the product now mints its own `CallRef` and stamps it into the dial
  string, so `bgapi`'s reply is only ever consulted for `-ERR`/success.
- **`voiceagent/telephony/freeswitch/media.py`**: `FreeSwitchMediaProvider`
  already correctly implements the `mod_audio_stream` wire envelope over an
  injected `MediaSocket` -- `register_socket()` was already documented as
  "the seam a real listener calls into," unbuilt until this phase.
- **`voiceagent/runtime/call_task.py`/`supervisor.py`**: `run_call_task()`'s
  own docstring/comments already named the exact gap this phase closes in
  part -- "media attachment/engine start is this phase's stand-in for
  'answered', since no real FreeSWITCH ANSWERED event is wired up yet."
  Confirmed by inspection: **nothing in this repository called
  `TelephonyProvider.events()` before this phase.** `CallRuntime` (one
  process, one event loop, many calls, `instance_id` per process) and
  `voiceagent.calls.lifecycle`'s transition table (`initiated, ringing,
  answered, in_progress, completed, failed, interrupted`, a same-state
  transition always a no-op) are both already sufficient and were not
  duplicated.
- **`voiceagent/runtime/reconciliation.py`**: deliberately does not
  implement ownership takeover -- a `CallSession` whose runtime's heartbeat
  expired is marked `interrupted`, never reassigned. This is the governing
  constraint for reconnect behavior (section 5).
- **`voiceagent/runtime/privacy.py`**: `authorize_call_data_access()` (the
  brief's own name, confirmed exact) is called exactly once per call, from
  `run_call_task()`, strictly before `media.attach()`/`engine.start()` --
  proven by an existing test
  (`test_run_call_task_denied_authorization_never_starts_the_engine`).
  Unmodified, and nothing in this phase's new media/event-routing code runs
  before it.
- **Tenant-correlation trust boundary**: confirmed to **not exist yet** for
  a FreeSWITCH-originated call -- `voiceagent/runtime/errors.py`'s own
  comment names an unbuilt "Call Orchestrator" as the caller responsible
  for tenant resolution. `PhoneNumber.e164` is schema-designed for a
  future reverse lookup (globally unique, RLS-exempt by design) but no
  resolver function exists. This phase does not build one -- see "Known
  limitations."
- **Metrics**: `voiceagent/metrics.py`'s `ProviderFamily` already includes
  `"telephony"`/`"media"`, and `provider.py`/`media.py` already call
  `record_provider_operation()` with bounded operation names -- no new
  metrics code was needed; new operations (`start_media_stream`,
  `stop_media_stream`) reuse the identical existing call.
- **Config/staging**: `FreeSwitchSettings` (Phase 2.19) already had
  `esl_host`/`esl_port`/`media_public_url`/`command_timeout_seconds`, no
  credential field (deliberate). `validate_deployment_readiness()` already
  required a secure `media_public_url` once `esl_host` was set.

## 2. Files created

- `voiceagent/telephony/freeswitch/esl_transport.py` -- real TCP `EslConnection`
  (`EslTcpConnection`) and a reconnecting wrapper (`ManagedEslConnection`).
- `voiceagent/telephony/freeswitch/media_transport.py` -- real `wss://`
  listener (`FreeSwitchMediaListener`, `serve_freeswitch_media()`), signed
  media tickets (`mint_media_ticket`/`verify_media_ticket`), and the
  `MediaSocket` adapter (`WebSocketMediaSocket`).
- `voiceagent/runtime/telephony_events.py` -- `TelephonyEventRouter`, the
  one-per-process demultiplexer from `TelephonyProvider.events()` to
  per-call bounded queues.
- Tests: `tests/telephony/freeswitch/test_esl_transport.py` (15),
  `tests/telephony/freeswitch/test_media_transport.py` (14),
  `tests/runtime/test_telephony_events.py` (6),
  `tests/runtime/test_call_task_telephony_events.py` (5).
- `docs/PHASE-2.21-FREESWITCH-TELEPHONY-INTEGRATION.md` (this file).

## 3. Files modified

- `voiceagent/telephony/freeswitch/provider.py` -- `originate()`/`transfer()`
  correlation fix (section 1); new `start_media_stream()`/
  `stop_media_stream()` methods (FreeSWITCH-specific, deliberately not on
  the vendor-neutral `TelephonyProvider` Protocol).
- `voiceagent/runtime/call_task.py` -- `CallTaskDependencies.telephony_events:
  TelephonyEventRouter | None = None` (opt-in, default preserves every
  existing caller's exact behavior); new
  `_run_pumps_with_remote_hangup_detection()` wrapping `_run_pumps()`
  unchanged, adding real remote-hangup detection only when a router is
  supplied.
- `voiceagent/config/settings.py` -- `FreeSwitchSettings.media_listen_host`/
  `.media_listen_port` (where `serve_freeswitch_media()` itself binds,
  distinct from the externally-reachable `media_public_url`).
- `voiceagent/config/validation.py` -- `_validate_telephony_secrets_present()`:
  fails closed under staging/production when FreeSWITCH is configured but
  `FREESWITCH_ESL_PASSWORD`/`FREESWITCH_MEDIA_TICKET_SECRET` are absent from
  the secret store.
- `scripts/run_call_runtime.py` -- starts the real transport
  (`_FreeSwitchTransport`) when `VOICEAGENT_FREESWITCH_ESL_HOST` is
  configured; unchanged (heartbeat-only) otherwise.
- `.env.staging.example`, `docker-compose.staging.yml` -- the two new
  secrets, the two new listen-address variables, an exposed media port.
- `tests/telephony/freeswitch/test_provider.py`,
  `test_provider_security.py`, `tests/config/test_settings.py`,
  `test_deployment_validation.py` -- updated/extended for the above (all
  detailed in section 9).

## 4. Existing abstractions reused (nothing duplicated)

`TelephonyProvider`/`MediaProvider`/`CallEvent`/`HangupCause` (contracts,
untouched), `FreeSwitchTelephonyProvider`/`FreeSwitchMediaProvider` (fixed,
not replaced), `voiceagent.calls.lifecycle`'s transition table and
`transition_call_session()` (the only place a `CallSession` status is ever
written -- this phase's new hangup path calls it exactly zero times itself,
relying entirely on `run_call_task()`'s own existing single finalization
call site), `CancellationSignal` (its own pre-existing `reason` field/
semantics, reused verbatim), `record_provider_operation()` (Phase 2.14,
unchanged), the bounded-queue-with-drop-on-full pattern from
`voiceagent.runtime.conversation_persistence` (mirrored exactly for both new
queue types), and the `infra.secrets` pattern every AI vendor key already
uses (for the two new FreeSWITCH secrets).

## 5. FreeSWITCH/ESL architecture

```text
mod_event_socket (real FreeSWITCH)
        |  TCP, inbound mode
        v
EslTcpConnection            (voiceagent/telephony/freeswitch/esl_transport.py)
  - real wire framing: header blocks, Content-Length bodies,
    Content-Type dispatch (auth/request, command/reply, api/response,
    text/event-plain, text/disconnect-notice)
  - commands serialized (one in flight at a time -- ESL gives no
    correlation id; a reply is matched to a command purely by wire order)
  - a command whose reply is abandoned (timeout/cancellation) poisons the
    connection rather than risking a later command receiving its stale
    reply (see `send()`'s own docstring; proven by
    test_a_wedged_command_times_out_and_closes_the_connection)
        |
        v
ManagedEslConnection          (same file)
  - bounded exponential-backoff reconnect
  - a command in flight during a disconnect fails immediately
    (TransportError), never hangs
  - carries NO channel-UUID memory across a reconnect (see section 11)
        |
        v
FreeSwitchTelephonyProvider  (voiceagent/telephony/freeswitch/provider.py,
                              unchanged apart from the section-1 fix)
        |
        v
TelephonyProvider (contract) -- everything above this line is FreeSWITCH-
                                 specific and confined to this one package
                                 (import-linter + AST-scan fence, verified
                                 unmodified and passing)
```

Credentials: `FREESWITCH_ESL_PASSWORD` is read fresh on every connection
attempt (including every reconnect) via a `password_provider` callable --
`ManagedEslConnection` never stores it. Production wiring
(`scripts/run_call_runtime.py`) sources it from `infra.secrets`, identically
to every AI vendor API key.

## 6. Media architecture

```text
FreeSWITCH mod_audio_stream
        |  wss:// (binary PCM in, JSON text out -- media.py's own envelope, unchanged)
        v
websockets.serve() handler        (media_transport.py:serve_freeswitch_media)
        |
        v
FreeSwitchMediaListener.handle_connection()
  1. extract_call_ref(path)  -- verifies a signed, short-lived ticket
     BEFORE anything else (section 9)
  2. on success: WebSocketMediaSocket wraps the real connection,
     media_provider.register_socket(call_ref, socket)
  3. on failure: close the connection immediately; register_socket()
     is never called for any call
        |
        v
FreeSwitchMediaProvider.attach()/detach()   (unchanged, existing code)
        |
        v
MediaProvider (contract)
```

**Trust boundary** (`docs/PHASE-0-ARCHITECTURE.md` §14.3, quoted verbatim in
`media_transport.py`'s own docstring): "the `<wss-url>` carries a short-lived
signed session ticket, not a tenant id." `mint_media_ticket(call_ref,
secret, ttl_seconds)` produces `<call_ref>.<expiry>.<hmac_hex>`;
`verify_media_ticket()` checks the HMAC with `hmac.compare_digest`
(constant-time) and the expiry, raising `TicketVerificationError` for
anything else -- a malformed path, a wrong signature, a tampered `call_ref`,
or an expired ticket are all rejected identically, before `register_socket()`
is ever called (proven by
`test_handle_connection_rejects_an_invalid_ticket_without_registering_a_socket`
and a real-`websockets`-client end-to-end equivalent).

Bounded, no blocking: `WebSocketMediaSocket.receive_binary()` is a thin
`async for` over the real connection's own `recv()`; a non-binary message
is dropped (mirrors `_normalize_event()`'s own "drop, don't raise" posture
for an unmapped event) rather than raised, so one malformed frame cannot
abort an otherwise-good stream. No database call anywhere in this module.

## 7. Lifecycle mapping

| Brief's transition | Status |
|---|---|
| originate/request | Unchanged: `FreeSwitchTelephonyProvider.originate()`, now correctly self-correlated (section 1). |
| answer | **Not gated on a real event in this phase** -- see "Known limitations." |
| media ready | No distinct internal status exists for this (the state machine has none); `media.attach()` remains the existing signal. |
| active (`in_progress`) | Unchanged, still follows `answered` synthetically. |
| hangup / remote termination | **New, real**: `TelephonyEventRouter` demultiplexes a real `CallEventType.HUNGUP` to the owning call's subscriber queue; `_run_pumps_with_remote_hangup_detection()` stops the pump and sets `cancellation.reason = "hangup"`, which `run_call_task()`'s own existing `_final_status()` already maps to `("completed", "completed")`. |
| local termination | Unchanged -- an application-initiated hangup already ends the pump via the media/engine side; the outer cancellation path is unaffected by this phase's addition (proven by `test_outer_cancellation_still_propagates_and_is_not_swallowed`). |
| failure | Unchanged -- `_final_status()`'s existing `"provider_disconnect"`/`"media_disconnect"` -> `failed` mapping is untouched and still reachable exactly as before. |
| cancellation | Unchanged supervisor-driven path, verified still correct with the new code present. |
| timeout | Unchanged (`command_timeout_seconds` bounds every ESL command; no new timeout concept was needed for lifecycle events). |
| shutdown | Unchanged `CallRuntime.shutdown()` path; `_FreeSwitchTransport.stop()` (the new transport-level shutdown) is independent and itself bounded (cancels the event-router task, closes the media server, closes the ESL connection, in that order). |

No second lifecycle state machine was created: the new hangup path never
calls `transition_call_session()` itself -- it only ever influences which
argument `run_call_task()`'s own single, pre-existing finalization call site
receives (`cancellation.reason`). Out-of-order/duplicate events are handled
by construction: `voiceagent.calls.lifecycle.is_valid_transition()` already
treats a same-state transition as a no-op, `TelephonyEventRouter` drops any
event for an unknown/already-unsubscribed `call_ref`, and the hangup watcher
is single-shot (it returns and is cancelled after the first `HUNGUP`, so a
duplicate delivered afterward reaches no consumer).

## 8. Security/privacy enforcement

- **Authorization is untouched and still strictly ordered first**:
  `authorize_call_data_access()` still runs before `media.attach()`/
  `engine.start()` in `run_call_task()`; nothing in this phase's new code
  runs earlier than that, and the new hangup-detection path only activates
  *after* the pump has already started (i.e., after authorization already
  succeeded).
- **A media connection cannot bypass authorization**: `handle_connection()`
  verifies the ticket before ever calling `register_socket()` -- an
  unauthorized/invalid connection never reaches `MediaProvider.attach()`
  for any call (tested, including over a real `websockets` client).
- **No credential/audio/transcript logging**: grepped every new file --
  the ESL password and media ticket secret are never interpolated into a
  log line or exception message anywhere; `_logger.warning()` calls name
  only fixed event labels, never event content, a frame's bytes, or a
  ticket value.
- **No cross-call media leakage**: each `WebSocketMediaSocket` wraps exactly
  one real connection, tickets are single-`call_ref`-scoped, and
  `FreeSwitchMediaProvider`'s existing per-`call_ref` dict is unchanged.
- **No tenant escape**: none of the new code (`esl_transport.py`,
  `media_transport.py`, `telephony_events.py`) references a tenant concept
  at all -- they operate purely on `call_ref`/`CallEvent`, with zero
  database access.

## 9. Concurrency/backpressure behavior

- **Still one process, one event loop, many calls** -- no new process,
  thread, or per-call event loop was introduced. `websockets.serve()` and
  `ManagedEslConnection`'s reconnect supervisor are both ordinary
  `asyncio` tasks within the same loop `CallRuntime` already runs in.
- **Every new queue is bounded**: `EslTcpConnection`'s internal
  control/event queues (`_QUEUE_MAXSIZE = 256`), `TelephonyEventRouter`'s
  per-subscriber queues (`_SUBSCRIBER_QUEUE_MAXSIZE = 32`) -- both drop the
  newest item and log on overflow, mirroring `conversation_persistence`'s
  established discipline exactly (verified by
  `test_a_full_subscriber_queue_drops_the_new_event_and_does_not_raise`).
- **No blocking operation anywhere in a new audio-adjacent path**: no
  database call, no synchronous I/O, in `esl_transport.py`,
  `media_transport.py`, or `telephony_events.py`.
- **Cancellation-safe throughout**: `asyncio.CancelledError` is never
  caught-and-discarded anywhere in the new code; every `except
  BaseException`/`except asyncio.CancelledError` block either re-raises or
  is a deliberate, tested, single-purpose absorption (the hangup-triggered
  case in `call_task.py`, proven not to shadow a genuine outer
  cancellation).
- **Concurrency demonstrated directly**:
  `test_concurrent_calls_never_cross_talk` (10 simulated concurrent calls
  through one shared `TelephonyEventRouter`, each receiving only its own
  events in order) and `test_concurrent_sends_are_serialized_not_interleaved`
  (three concurrent ESL commands against one real fake-server connection,
  proven not to cross-deliver replies).

## 10. Tests added and results

40 new tests across the 4 new files (`test_esl_transport.py`: 15,
`test_media_transport.py`: 14, `test_telephony_events.py`: 6,
`test_call_task_telephony_events.py`: 5), plus 10 more added to existing
files: `tests/telephony/freeswitch/test_provider.py` (+5, the correlation
fix and the two new media-stream commands), `test_provider_security.py`
(+1, `transfer()`'s new validation), `tests/config/test_settings.py` (+3,
the two new listen-address fields), `tests/config/test_deployment_validation.py`
(+2, the two new required secrets); one existing test
(`test_freeswitch_with_secure_media_public_url_passes`) was updated, not
replaced, to also supply the two new required secrets. Net: 50 new tests
(862 -> 912, section 13). Coverage against the brief's own checklist
(section 15):

- **ESL**: successful connection, authentication failure, command success,
  command timeout, command failure (`-ERR`), disconnect (clean notice and
  abrupt), malformed event (bad `Content-Length`), cancellation during
  send, concurrent-send serialization, reconnect after disconnect,
  fail-fast while disconnected.
- **Lifecycle**: remote hangup stops the pump and finalizes `completed`;
  events for a different `call_ref` never affect this call; outer
  cancellation still propagates unchanged; unsubscribe happens on normal
  completion; the `telephony_events=None` regression case is
  indistinguishable from calling `_run_pumps()` directly.
- **Correlation**: `originate()`/`transfer()` mint and return their own
  `CallRef` without parsing the ESL reply; an unknown/stale `call_ref`'s
  event is dropped, delivered to no subscriber.
- **Media**: real inbound binary passthrough, real outbound JSON-enveloped
  send, bounded queue behavior (router-side), closed/rejected-ticket
  session protection, cancellation-safety (transport-level, via the ESL
  tests' identical pattern), malformed ticket handling, a real
  `websockets` client/server disconnect.
- **Security**: unauthorized media connection rejected before
  `register_socket()`; tampered/expired/wrong-secret tickets all rejected;
  no credential ever logged (verified by reading every new file, not
  merely asserted).
- **Concurrency**: `test_concurrent_calls_never_cross_talk`,
  `test_concurrent_sends_are_serialized_not_interleaved`.

All new and pre-existing tests pass together -- see section 13.

## 11. Live FreeSWITCH validation

**No real FreeSWITCH instance is available in this environment.** Per the
brief's own section 16 ("do NOT fabricate successful live validation"),
this is reported as a limitation, not worked around:

- **Hermetic (fully verified)**: every ESL/media test runs against a real
  `asyncio` TCP server (`_FakeFreeSwitchServer`) speaking the actual wire
  framing byte-for-byte, and a real `websockets` client/server pair on
  `localhost` -- not `Protocol`-level fakes. This is deliberately stronger
  than the pre-existing `FakeEslConnection`/`FakeMediaSocket` doubles (which
  still exist, unmodified, and still back every other existing telephony
  test).
- **Live composition-root verification (real sockets, no live FreeSWITCH)**:
  `scripts/run_call_runtime.py`'s new `_FreeSwitchTransport` class was
  exercised end-to-end against the same real fake TCP server used in the
  test suite -- real connect, real auth, real event subscription, a real
  bound `wss://` listener, a real event routed from the fake server through
  to a subscriber -- proving the composition root's own wiring (not just
  the individual classes in isolation). Output: `STARTED OK: ['auth secret',
  'event plain ALL']`, `EVENT ROUTED OK: answered`, `STOPPED OK`.
- **Not validated, and not claimed**: a real ESL wire exchange against
  actual FreeSWITCH software, real SIP/RTP call setup, real
  `mod_audio_stream` binary framing from a live media bridge, and the
  `bgapi originate {origination_uuid=...}` channel-variable behavior this
  phase's correlation fix depends on (documented ESL behavior, not
  independently re-verified against a live server in this phase --
  carrying forward the identical, explicit caveat `provider.py`'s own
  docstring has stated since Phase 2.2).

## 12. Known limitations / deferred items

- **Real ANSWERED-event-gated call start is deferred.** `run_call_task()`
  still transitions `answered` -> `in_progress` synthetically, immediately
  after media attach/engine start, rather than waiting for a real
  `CHANNEL_ANSWER` event. Making that change safely requires updating the
  call-start sequencing and every existing hermetic test that exercises
  `run_call_task()`'s startup path (a large, cross-cutting ripple this
  phase's own scope restrictions -- "do not redesign `CallRuntime`," "make
  the smallest architectural correction necessary" -- argue against
  bundling with the transport work above). What *is* real and shipped this
  phase is remote-hangup detection (section 7), the highest-value single
  lifecycle event for correct resource cleanup.
- **No "Call Orchestrator" exists.** Inbound-call tenant/DID resolution
  (`PhoneNumber.e164`'s reverse lookup), outbound-call origination
  requests, and `CallSession` creation from a real telephony event remain
  entirely unbuilt -- confirmed, by two independent lines of inspection, to
  have been unbuilt before this phase too (`voiceagent/runtime/errors.py`'s
  own comment; zero existing callers of `TelephonyProvider.events()`).
  `_FreeSwitchTransport` (this phase) constructs and runs the real
  transport, ready for that future component to use, but nothing yet calls
  `CallRuntime.start_call()` from it. Building the orchestrator is a
  separate, substantial feature (agent/DID resolution, entitlement and
  concurrency checks, `CallSession` creation) explicitly out of a
  transport-integration phase's scope.
- **Parked-channel reaper / two-layer concurrency reconciliation deferred.**
  `docs/PHASE-0-ARCHITECTURE.md` §10.6 names this "design-in, not retrofit"
  future work; `voiceagent.runtime.reconciliation`'s own module docstring
  already establishes no session-takeover mechanism exists to reconcile
  against. `ManagedEslConnection`'s reconnect is bounded and safe (no
  cross-reconnect channel memory, so nothing *can* be duplicated), but does
  not perform active channel/session reconciliation on reconnect.
- **`bgapi`/`origination_uuid` semantics carry forward the same
  not-independently-verified caveat** every FreeSWITCH command string in
  this codebase has had since Phase 2.2 -- documented ESL behavior, not
  exercised against a live server (section 11).
- **`start_media_stream()`/`stop_media_stream()` are not yet called by
  anything** -- they exist as the correct, tested primitive for a future
  orchestrator (or a manual operator action) to issue the ESL command that
  makes FreeSWITCH actually connect to the new `wss://` listener; nothing
  in this repository triggers that today, for the same reason no
  orchestrator exists yet.

## 13. Quality-gate results

- Backend tests: **912 passed**, 241 deselected, 0 failed (Phase 2.20's 862
  plus 50 net new: 62 new tests across the 4 new files and updated existing
  files, minus none removed).
- Ruff: clean. Ruff format: clean.
- Pyright: 0 errors, 0 warnings (on `voiceagent/`/`tests/`, its configured
  scope -- `scripts/*.py` is outside pyright's configured `include`, matching
  every other script in this repository; verified instead by direct import
  and the live composition-root check in section 11).
- import-linter: 8/8 contracts kept (199 files, 956 dependencies) -- the
  FreeSWITCH-confinement contract covers both new
  `voiceagent/telephony/freeswitch/*.py` files automatically; neither is
  named anywhere outside that package.
- detect-secrets: clean; one new fake test-secret literal carries a
  documented `pragma: allowlist secret` (identical pattern to every prior
  phase's own test secrets); baseline itself untouched.
- pip-audit: no known vulnerabilities (`saas-os`/`voiceagent` correctly
  skipped, not on PyPI).
- Frontend: untouched, not re-run (no frontend file changed).

## 14. SaaS-OS pin verification

Unchanged: `ff550010e5eafecace7311038aadc99fcecfbe3d`
(`pyproject.toml` diff: none).

## 15. Final report items

See the end-of-phase report for `git status --short`, protected-file
confirmation, and the no-commit/no-push confirmation.
