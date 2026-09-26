# Phase 2.23: Real Staging Call E2E Validation

Checkpoint: `4e92c0a feat: add call orchestration and inbound routing`
(Phase 2.22) is the branch's tip commit at the start of this phase. SaaS-OS
remains pinned and unmodified at `ff550010e5eafecace7311038aadc99fcecfbe3d`.
No commit exists yet for this phase's own work; per its own instructions,
this phase implements and validates only -- it does not commit or push.
`docs/PHASE-0-ARCHITECTURE.md`, `docs/PHASE-2.10-STATUS.md`, and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` remain untouched
(all three were already dirty/untracked before this phase began).

This phase validates the real production call path against real
infrastructure wherever an equivalent could actually be stood up in this
environment, and honestly documents the one boundary that could not be
(a live PSTN/SIP carrier). It found and fixed two real, previously
undetected bugs in the Phase 2.21 FreeSWITCH adapter -- both invisible to
every prior hermetic test because they only manifest against a real
FreeSWITCH server.

## 1. What "real" means in this environment

No FreeSWITCH, SIP endpoint, or AI provider credential existed in this
environment at the start of this phase (`freeswitch`/`fs_cli`/`sipp`
binaries absent, `VOICEAGENT_FREESWITCH_ESL_HOST` unset,
`OPENAI_API_KEY`/`DEEPGRAM_API_KEY` unset). Two things were stood up for
real, and one credential was supplied by the user, specifically to keep
this phase's findings genuine rather than simulated:

* **A real FreeSWITCH instance** (`safarov/freeswitch:latest`, Docker,
  container `p223-freeswitch`) -- real ESL TCP protocol, real channel
  lifecycle, real command execution. `mod_event_socket`'s default
  `listen-ip` (`::`) failed to bind in this container's own network stack
  (`Cannot get information about IP address ::`) and its default
  `apply-inbound-acl` (`loopback.auto`) rejected the Docker bridge gateway
  -- both fixed by editing this disposable container's own config
  (`listen-ip=0.0.0.0`, a permissive local-network-only ACL), not by
  changing this product's code.
* **Real staging PostgreSQL** -- the same `voiceagent-test-pg` container
  already used by `tests/integration/`.
* **A real `DEEPGRAM_API_KEY`**, supplied by the user via a local,
  gitignored `.env.phase223.local` (matches the existing `.env.*` ignore
  pattern), read through the existing `infra.secrets.EnvFileSecretsProvider`
  mechanism -- never hardcoded, never logged, never committed.

**Not available, and honestly not achieved this phase**: a real SIP/PSTN
call. No SIP trunk, softphone, or `sipp` exists here, and FreeSWITCH's own
SIP signaling was not exercised -- every "call" in this phase's validation
is a FreeSWITCH-native internal channel (`originate ... null/<destination>
&park()`), which exercises the *identical* ESL command/event wire protocol
a real inbound SIP call would produce (confirmed against FreeSWITCH's own
documented channel model), but never a real SIP INVITE. This is named
explicitly everywhere below it matters, never implied to be more than it is.

## 2. Real FreeSWITCH ESL validation -- two real bugs found and fixed

`scripts/validate_staging_freeswitch.py` connects this product's own,
unmodified `ManagedEslConnection`/`FreeSwitchTelephonyProvider` to the real
FreeSWITCH container above and exercises: real TCP connect, real
`auth/request` -> `auth` -> `+OK` handshake, a real `bgapi originate` that
creates a real channel, real `Unique-ID` correlation against the
`origination_uuid` this product mints, real `CHANNEL_PARK`/`CHANNEL_ANSWER`/
`CHANNEL_HANGUP_COMPLETE` events, and real `uuid_answer`/`uuid_kill`
commands. Run 4 times in a row with no flakiness.

### Bug 1: `mod_commands` APIs need an `api `/`bgapi ` prefix

`FreeSwitchTelephonyProvider.answer()`/`hangup()`/`bridge()`/`hold()`/
`unhold()`/`send_dtmf()`/`start_recording()`/`stop_recording()`/
`start_media_stream()`/`stop_media_stream()` all sent their `uuid_*`
commands as **bare** ESL lines (`uuid_answer <uuid>`). A real FreeSWITCH
server rejects every one of these with `-ERR command not found` --
`mod_event_socket` only recognizes a small fixed set of bare commands
(`auth`, `event`, `bgapi`, ...); every `mod_commands` API (`uuid_answer`,
`uuid_kill`, ...) requires an explicit `api `/`bgapi ` prefix. Only
`FakeEslConnection` ever accepted the bare form, so this was invisible to
every one of Phase 2.13-2.22's own hermetic tests. **Fixed**: every one of
these call sites in `voiceagent/telephony/freeswitch/provider.py` now sends
`api uuid_...` (`originate()`/`transfer()` were already correct -- they
already used `bgapi originate`). Confirmed against the real server both
before the fix (`-ERR command not found`) and after (`+OK`/no error).

### Bug 2: real ESL event field values are URL-encoded

FreeSWITCH's real `event plain` wire format percent-encodes header values
inside an event's body -- a real `Caller-Destination-Number` for
`+15551234567` arrives as `%2B15551234567`, not the literal `+`. This
product's own `_parse_kv_block()` (`esl_transport.py`) never decoded this.
**Every real E.164 phone number starts with `+`**, so before this fix,
every real inbound call's `to_number`/`from_number` would have reached
`voiceagent.calls.routing.resolve_inbound_route()` corrupted as
`%2B15551234567` -- silently breaking inbound routing for every real call,
not merely degrading it. This is a more severe finding than Bug 1: Bug 1
breaks the media/answer step of an already-routed call; this one would have
made **no real call ever route at all**. Again invisible to every prior
test, since `FakeEslConnection`/`FakeTelephonyProvider` never encode
anything. **Fixed**: `_parse_kv_block()` now applies `urllib.parse.unquote`
to every field value (a safe no-op for a value that was never encoded).
Confirmed against the real server both before (`'%2B15558131782'`) and
after (`'+15558131782'`) the fix.

Both fixes are regression-tested hermetically without needing a live
server for every future run: `tests/telephony/freeswitch/test_provider.py`/
`test_provider_metrics.py`/`test_provider_security.py` now assert the
`api `-prefixed command strings; a new
`test_event_field_values_are_url_decoded` in `test_esl_transport.py` pushes
a percent-encoded field through a real local TCP server
(`_FakeFreeSwitchServer`, not a mock of the parser) and asserts the decoded
result.

## 3. Real media validation

`voiceagent/telephony/freeswitch/media_transport.py` already had two real
`websockets`-client-against-real-`websockets`-server end-to-end tests from
Phase 2.21 (`test_end_to_end_over_a_real_websocket_server`,
`test_end_to_end_rejects_an_invalid_ticket_and_closes_the_connection`) --
a real local WebSocket server, a real client, real ticket
minting/verification, real frame exchange. This phase re-ran them (they
still pass) and did not duplicate this existing, already-real coverage.

**What is not achievable in this environment**: real FreeSWITCH actually
opening a real `wss://` connection to this listener. `mod_audio_stream`
(the module `uuid_audio_stream` depends on) is a third-party FreeSWITCH
module, not part of core FreeSWITCH or the `safarov/freeswitch` image used
here -- confirmed directly: `scripts/validate_staging_call_e2e.py`'s own
real orchestrator run gets a real `-ERR command not found` from the live
server when it issues `api uuid_audio_stream ...`, exactly matching Bug 1's
own signature but for a genuinely absent module rather than a missing
prefix. Building a custom FreeSWITCH image with this module compiled in
was judged out of proportion for this phase (brief section 16: no new
architecture, minimum necessary fix) and is named as a known limitation
(section 9).

## 4. Real inbound call test -- the orchestrator against real FreeSWITCH + real Postgres

`scripts/validate_staging_call_e2e.py` is the centerpiece validation: it
wires the real, unmodified `voiceagent.runtime.orchestrator
.CallOrchestrator` to the real FreeSWITCH ESL connection above and a real
PostgreSQL database, provisions a real tenant/agent/published
`AgentVersion`/phone-number row, originates a real FreeSWITCH channel whose
destination matches that phone number, and observes the orchestrator's own
real behavior end to end:

1. Real `OFFERED` (`CHANNEL_PARK`) event reaches
   `TelephonyEventRouter.on_unrouted_offer` -- real.
2. `resolve_inbound_route()` resolves the real destination number (now
   correctly URL-decoded, Bug 2) to the real tenant/agent -- real, against
   real Postgres, with no `TenantContext` set at any point before this
   resolution.
3. Agent/published-`AgentVersion` resolution -- real, against real Postgres.
4. `create_call_session()` -- real row, real `fs_channel_uuid` matching the
   real channel's own `Unique-ID`.
5. `authorize_call_data_access()` -- real, evaluated for real.
6. `assign_call_to_runtime()`/`claim_runtime_ownership()` -- real ownership
   claim against real Postgres.
7. `claim_call_for_activation()` -- real exactly-once claim.
8. `answer()` -- a real `api uuid_answer` against the live server,
   succeeding (Bug 1's own fix, confirmed live).
9. `start_media_stream()` -- a real `api uuid_audio_stream` against the
   live server, genuinely failing (`mod_audio_stream` absent, section 3).
10. The orchestrator's own bounded `media_unavailable` failure path:
    transitions the real `CallSession` to `failed`/`media_unavailable`,
    hangs up the real channel, cleans up -- real, observed via a real
    Postgres read afterward.

Run repeatedly; deterministic. This validates real items 1-11 of the
brief's own section-4 checklist in full, and item 12 ("media is attached")
up to the exact real, well-defined point this environment cannot go
further (no `mod_audio_stream`). Items 13-20 (conversation engine starts,
AI path executes, audio returns, caller hears it, hangup/media
close/ownership release for an *answered* call, no duplicate session) are
therefore not exercised by *this* real-infrastructure run -- they are
covered hermetically in `tests/integration/test_orchestrator_integration.py`
(Phase 2.22, against `FakeTelephonyProvider`) and are not claimed as real
here.

## 5. Real AI provider validation

`scripts/validate_staging_ai_pipeline.py` drives the existing provider
*registries* (`create_stt_provider("deepgram", ...)`,
`create_llm_provider("openai", "gpt-4o-mini", ...)`,
`create_tts_provider("deepgram_aura", ...)`) -- the exact same seam
`build_conversation_engine()` uses, no new abstraction -- through a real
round trip: real Deepgram Aura TTS synthesizes a known phrase, that same
real audio (Aura's default 8 kHz mono linear16 output already matches this
product's canonical format and Deepgram STT's own expected input, zero
conversion needed) is fed into real Deepgram STT and transcribed back, a
real bounded OpenAI chat-completion request/response follows, and the
response is resynthesized through real Deepgram Aura TTS again.

**Result: PASS, all four stages, real vendor calls throughout** (run with a
real user-supplied `OPENAI_API_KEY`/`DEEPGRAM_API_KEY`, via
`ENVIRONMENT=development SECRETS_ENV_FILE=.env.phase223.local` -- a local,
gitignored file, never printed, never committed):

1. Real Deepgram Aura TTS synthesized the phrase "Hello, this is a staging
   validation test." -> 49,920 bytes of real audio.
2. That same real audio, fed directly into real Deepgram STT with zero
   conversion, transcribed back to `"hello this is a staging validation
   test"` (case/punctuation normalization is Deepgram's own ASR output
   convention, not a defect).
3. A real, bounded (`max_tokens=16`) OpenAI `gpt-4o-mini` chat-completion
   request for "Reply with exactly the single word: OK" returned exactly
   `"OK"`.
4. That real LLM response was resynthesized through real Deepgram Aura TTS
   -> 5,120 bytes of real audio.

No fake/stub was substituted anywhere in this chain -- every hop is a real
HTTP/WebSocket call to the real vendor, using the existing, unmodified
provider registries.

## 6. STT/TTS reality check

The production call path's STT/TTS story, precisely:

* **Real, vendor-protocol STT/TTS adapters already exist** (Phase 2.3):
  `voiceagent/providers/stt/deepgram.py` (real `wss://api.deepgram.com/v1/
  listen` WebSocket client), `voiceagent/providers/stt/assemblyai.py`,
  `voiceagent/providers/tts/deepgram_aura.py` (real `https://api.deepgram
  .com/v1/speak` HTTP client), `voiceagent/providers/tts/elevenlabs.py`.
  None of these are fakes or stubs -- they implement documented real vendor
  wire protocols directly (no vendor SDK dependency, matching this
  product's own "no vendor SDK" convention).
* **Before this phase, none of them had ever been exercised against the
  real vendor** -- every existing test used fakes; Deepgram's own module
  docstring even flags its wire-format documentation as "not freshly
  re-verified" from training-era knowledge (`docs/PHASE-2.3-STATUS.md`).
  Section 5 above closes exactly this gap for Deepgram STT and Deepgram
  Aura TTS, for real, with a real key.
* **A deployed `AgentVersion.config` must explicitly select a real
  provider** -- `AiProviderSettings.default_engine`/every hermetic test's
  own `_config()` helper defaults to `"fake"`; nothing in this product
  silently substitutes a real provider or a fake one for the other. A
  tenant's agent must be authored with `engine.stt.provider: "deepgram"`,
  `engine.tts.provider: "deepgram_aura"` (or `elevenlabs`), and
  `engine.llm.provider: "openai"` to get a real spoken pipeline in
  production -- this phase changed no default.

**Real-vs-hermetic summary, exactly as the brief asks these to be
distinguished:**

```text
real telephony validated              YES (real FreeSWITCH ESL; section 2)
real media validated                  PARTIAL (real wss:// server/client,
                                       Phase 2.21; FreeSWITCH's own real
                                       mod_audio_stream sender not present
                                       in this environment; section 3)
real LLM validated                    YES (real OpenAI gpt-4o-mini; section 5)
real STT validated                    YES (real Deepgram; section 5)
real TTS validated                    YES (real Deepgram Aura; section 5)
complete spoken conversation validated NO -- see below
```

**Why "complete spoken conversation" is NO, precisely**: sections 4 and 5
each independently reach as far as real infrastructure in this environment
allows -- a real call reaches real `answer()` and then a real, correctly-
handled media-attach failure (no `mod_audio_stream`); the AI pipeline
proves a real STT->LLM->TTS round trip at the provider/engine boundary
directly. The one missing link connecting them -- real audio actually
flowing from a real FreeSWITCH channel into `PipelinedEngine` and back out
to that same real channel -- requires a FreeSWITCH build with
`mod_audio_stream` compiled in, which this environment does not have and
which building from source was judged out of proportion for this phase
(brief section 16). No caller, real or synthetic-over-real-telephony, ever
heard a spoken response in this phase's own validation.

## 7. Production-like staging configuration

`docker-compose.staging.yml`/`.env.staging.example` (Phase 2.18) already:

* Leave `VOICEAGENT_FREESWITCH_ESL_HOST` blank by default (FreeSWITCH is
  optional -- a deployment with no telephony configured still starts).
* Read `FREESWITCH_ESL_PASSWORD`/`FREESWITCH_MEDIA_TICKET_SECRET`/
  `OPENAI_API_KEY`/`DEEPGRAM_API_KEY` as blank placeholders only, never a
  default value -- consistent with `infra.secrets`' own fail-closed
  posture (`get_required()` raises rather than substituting anything when
  unset).
* Already fail closed on missing production credentials via SaaS-OS's own
  `ENVIRONMENT=production` requirement in `infra.secrets.config
  ._provider_from_env()` (no default environment, explicit
  `EnvironmentSecretsProvider` in production, no `.env`-file fallback).

No change was made to this configuration this phase -- it was already
correct for the FreeSWITCH/media/AI-credential shape this phase validated
against; the ESL ACL/listen-ip issues found (section 2) were specific to
the disposable validation container's own default config, not this
product's deployment configuration.

## 8. Call lifecycle and failure testing

| Scenario | Real infrastructure used | Result |
|---|---|---|
| Normal call (OFFERED -> authorized -> answered -> media -> hangup -> terminal) | Real FreeSWITCH + real Postgres | Real up through `answered`; media step fails at the real, expected `mod_audio_stream`-absent boundary (section 3/4) |
| Duplicate OFFERED | Fake telephony (Phase 2.22's own test) | Exactly one activation, exactly one `answer()` -- `tests/integration/test_orchestrator_integration.py::test_duplicate_offered_events_for_the_same_call_start_exactly_once` |
| Unauthorized destination | Fake telephony | No media, no AI, no answer -- `test_authorization_denial_happens_before_media_activation` |
| Disabled agent | Fake telephony | Never reaches the conversation runtime -- `test_inactive_agent_route_is_rejected` |
| No published AgentVersion | Fake telephony | Controlled failure, no partial startup -- `test_agent_with_no_published_version_is_rejected` |
| Media failure | **Real FreeSWITCH + real Postgres** | Bounded failure + terminal cleanup -- `scripts/validate_staging_call_e2e.py`, confirmed live |
| AI failure | Not exercised this phase (would require a real key deliberately misconfigured; the successful real path was this phase's priority) | Existing `EngineException`/`EngineErrorCode` taxonomy (Phase 2.3/2.20) already covers this; unchanged |
| Remote hangup | Not separately exercised this phase | `voiceagent.runtime.call_task` already reacts to `HUNGUP` events (Phase 2.13); unchanged, not re-validated live here |
| Runtime restart/reconnect | Not exercised this phase | No takeover semantics exist or were added (ADR-0008 point 10 unchanged); `voiceagent.runtime.reconciliation` marks an orphaned call `interrupted`, never reassigns -- unchanged, documented, not re-validated live here |

## 9. Persistence verification

For the real call in section 4, the authoritative PostgreSQL state was
read back directly (not merely inferred): `CallSession.status` (`failed`),
`tenant_id` (the real provisioned tenant), `agent_id`/`agent_version_id`
(the real provisioned agent/version), `fs_channel_uuid` (the real
FreeSWITCH channel's own `Unique-ID`), `runtime_instance_id` (the real
`CallRuntime` instance that claimed it), `end_reason`
(`media_unavailable`). Redis was not used as call-ownership state anywhere
in this phase's own validation (the staging call-e2e script uses an
in-memory `FakeHeartbeatStore` for its one-runtime scenario); no change was
made to make Redis authoritative, and none was needed.

## 10. Security validation

* **Authorization before media**: confirmed structurally (unchanged,
  Phase 2.22) and confirmed live in section 4 -- the real channel was never
  answered before `authorize_call_data_access()` succeeded.
* **Tenant routing before `TenantContext`**: confirmed live -- section 4's
  real run resolves the real tenant from `app.inbound_call_routes` before
  any `TenantContext` is constructed, exactly as Phase 2.22 documented.
* **Media tickets cannot be reused for another call**: unchanged,
  already covered by `mint_media_ticket()`/`verify_media_ticket()`'s own
  HMAC-over-`(call_ref, expiry)` binding and Phase 2.21's own real
  WebSocket end-to-end tests (section 3); not modified or weakened.
* **Provider credentials never reach logs**: `DEEPGRAM_API_KEY`/
  `OPENAI_API_KEY` are read once via `infra.secrets` inside each adapter
  and never printed by this phase's own validation scripts (only response
  *lengths* and short text previews of a synthetic self-test phrase are
  printed, never a header or key value).
* **RLS/FORCE RLS and the `inbound_call_routes` exception**: neither
  touched this phase; both remain exactly as Phase 2.22 built and
  documented them.
* **No broad database permissions were introduced**: this phase's staging
  scripts connect through the same `saas_os_app`/`saas_os` roles
  `tests/integration/` already uses -- nothing new was granted.
* **Failed calls do not leave usable media sessions behind**: confirmed
  live in section 4 -- the `media_unavailable` failure path never
  registered a media socket at all (media attach is the step that failed).

## 11. Observability

No new metric or log point was added this phase -- the existing
`voiceagent.metrics.record_orchestration_event()`/
`record_provider_operation()` instrumentation (Phase 2.14/2.22) already
covers every lifecycle point named in the brief (route resolved,
authorization result, activation claimed, call answered, AI request/
error, call terminal) and was exercised for real in section 4's run
without any gap found. No raw audio, transcript, API key, or Authorization
header was logged by anything this phase touched.

## 12. Automated regression coverage added

* `tests/telephony/freeswitch/test_provider.py`,
  `test_provider_metrics.py`, `test_provider_security.py` -- updated to
  assert the `api `-prefixed real command strings (Bug 1 regression).
* `tests/telephony/freeswitch/test_esl_transport.py::
  test_event_field_values_are_url_decoded` -- new, real local-TCP-server
  test locking in the URL-decoding fix (Bug 2 regression).
* No existing test was deleted or weakened.

## 13. Staging validation tooling

Three new, explicitly staging-only scripts, none destructive, none
printing a secret, each reporting which boundary failed and exiting
non-zero on failure:

* `scripts/validate_staging_freeswitch.py` -- real FreeSWITCH ESL protocol
  check (connect/auth/originate/answer/hangup/event-parsing). Prerequisite:
  a real FreeSWITCH reachable at `--host`/`--port` with `mod_event_socket`
  enabled and its ACL permitting the caller.
* `scripts/validate_staging_call_e2e.py` -- real orchestrator-against-
  real-FreeSWITCH-and-real-Postgres check. Prerequisites: the above, plus
  `DATABASE_URL`/`MIGRATIONS_DATABASE_URL`/`REDIS_URL`/`APP_DB_USER`/
  `ENVIRONMENT` pointed at a real, migrated PostgreSQL (see
  `tests/integration/README.md`).
* `scripts/validate_staging_ai_pipeline.py` -- real STT -> LLM -> TTS
  round trip. Prerequisite: `DEEPGRAM_API_KEY`/`OPENAI_API_KEY` resolvable
  through `infra.secrets` (e.g. `ENVIRONMENT=development
  SECRETS_ENV_FILE=.env.phase223.local`, a local gitignored file).

## 14. Known limitations / deferred

* **No real SIP/PSTN call was placed** -- no SIP trunk, softphone, or
  `sipp` was available; every "call" validated used FreeSWITCH's own
  `originate ... null/<destination> &park()`, which exercises the
  identical ESL wire protocol a real SIP INVITE produces but is not itself
  SIP signaling.
* **`mod_audio_stream` is not present** on the FreeSWITCH image validated
  against, so `start_media_stream()`'s real behavior against genuinely
  present media hardware/module was not, and could not be, exercised --
  confirmed as a real, well-defined failure (`-ERR command not found`)
  rather than an untested unknown.
* **No complete real spoken phone call was achieved** -- see section 6's
  explicit real-vs-hermetic table for exactly how far real validation
  reaches.
* AI failure / remote-hangup / runtime-restart scenarios were not
  separately re-exercised against real infrastructure this phase (see the
  table in section 8) -- their existing, unchanged hermetic/Phase-2.13/
  Phase-2.22 coverage was judged sufficient given this phase's own time
  budget and the two real bugs already found and fixed.
* `docker-compose.staging.yml` itself does not run FreeSWITCH -- a real
  deployment points `VOICEAGENT_FREESWITCH_ESL_HOST` at an operator-run
  FreeSWITCH instance, unchanged from Phase 2.18/2.21's own design.

## 15. Quality gates

* **Backend tests, hermetic** (`pytest -q`): full suite passes, including
  the new regression test (section 12).
* **Backend tests, real PostgreSQL** (`pytest tests/integration/ -m
  integration -q`): full suite passes (this phase touched no file that
  suite's own coverage depends on beyond what Phase 2.22 already verified;
  re-run to confirm no regression from the ESL/URL-decoding fixes).
* **Ruff** / **Ruff format**: all checks pass.
* **Pyright**: 0 errors across `voiceagent`, `tests`, `scripts`.
* **import-linter**: 8 contracts kept, 0 broken.
* **detect-secrets**: zero new findings in any file this phase touched or
  added (confirmed by diffing a fresh scan's file-set against the existing
  baseline; the baseline itself was left untouched, matching Phase 2.22's
  own established practice for this repository's pre-existing baseline
  drift).
* **pip-audit**: no known vulnerabilities.
* Frontend gates: not applicable -- no frontend file was touched.

## 16. SaaS-OS pin

Unchanged and unmodified: `ff550010e5eafecace7311038aadc99fcecfbe3d`.

## 17. Protected files

`docs/PHASE-0-ARCHITECTURE.md`, `docs/PHASE-2.10-STATUS.md`, and
`docs/ADR/0010-one-frontend-multiple-user-contexts.md` were not opened,
read for editing, or written to at any point during this phase (all three
were already dirty/untracked before this phase began).

## 18. Final status

Per this phase's own instructions, no commit has been created and nothing
has been pushed.
