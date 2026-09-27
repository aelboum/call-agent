#!/usr/bin/env python
"""Phase 2.25: real SIP caller -> real FreeSWITCH -> real orchestrator ->
real Deepgram STT -> real OpenAI LLM -> real Deepgram Aura TTS -> real
FreeSWITCH -> real SIP caller, end to end.

**Staging-only.** Closes the one gap Phase 2.24 documented and could not
close: a genuine SIP/RTP caller. Everything else this script wires
together (`CallOrchestrator`, `FreeSwitchTelephonyProvider`,
`FreeSwitchMediaProvider`/`FreeSwitchMediaListener`, real PostgreSQL, real
AI provider registries) is the exact same, unmodified product code
`scripts/validate_staging_call_e2e.py` (Phase 2.23/2.24) and
`scripts/validate_staging_ai_pipeline.py` (Phase 2.23) already validated
for real -- this script's only new ingredient is `scripts/sip_uac.py`, a
small hand-rolled real SIP UAC (see that module's own docstring for why),
used here in place of an ESL `bgapi originate` so the call this script
drives is a **genuine inbound SIP INVITE with genuine RTP**, not an
ESL-only synthetic channel.

What real, empirical evidence this script produces, per real SIP call:

1. A real `INVITE` reaches FreeSWITCH's own `external` SIP profile and is
   real-dialplan-routed (`context=public`, this phase's own throwaway
   `01_phase225_e2e.xml` extension -- see
   docs/PHASE-2.25-REAL-SIP-SPOKEN-E2E.md section 2) to `park`, exactly
   like a genuine ringing inbound call: *not* answered until this
   product's own orchestrator explicitly calls `uuid_answer`, after real
   routing/authorization/ownership database round-trips complete. This is
   the real test of the Phase 2.24 ANSWERED-race question (section 8 of
   that phase's own doc) that a `null`-endpoint channel could not provide.
2. A real `200 OK` with a real SDP answer, a real `ACK`, and real
   bidirectional RTP (PCMU) between this script's own SIP UAC and the real
   FreeSWITCH server.
3. A real caller utterance (synthesized once, in advance, through the same
   real Deepgram Aura TTS this product uses -- a normal, honest way to
   drive a repeatable automated telephony test with genuine audio, not
   fabricated silence) is streamed into that real RTP session while the
   real `CallSession` is `in_progress` (media attached).
4. That real audio crosses real FreeSWITCH, is picked up by the real,
   unmodified `mod_audio_stream`/`FreeSwitchMediaProvider` media path
   (Phase 2.24), reaches the real `PipelinedEngineSession`'s real Deepgram
   STT, produces a real transcript, drives a real bounded OpenAI
   chat-completion against this script's own deterministic agent
   instructions, and is spoken back by real Deepgram Aura TTS.
5. That real synthesized reply crosses real FreeSWITCH again and arrives
   back at this script's own SIP UAC as real inbound RTP -- captured here
   and independently re-transcribed through a *second*, separate real
   Deepgram STT call (not the app's own) as this script's own proof that
   real, intelligible speech reached the far end of a real phone call, not
   silence or noise.
6. The SIP UAC then sends a real `BYE` (caller hangs up); this script
   confirms the real `CallSession` reaches a real terminal status.

Exit code 0 only if every one of the stages above was empirically observed
for real -- this script's own `FAIL:` branches name exactly which boundary
did not hold, and it never prints `PASS` for a stage it did not actually
exercise (see docs/PHASE-2.25-REAL-SIP-SPOKEN-E2E.md, which this script's
own output backs, line for line).

Prerequisites (all documented in docs/PHASE-2.25-REAL-SIP-SPOKEN-E2E.md):

* Real PostgreSQL (`DATABASE_URL`/`MIGRATIONS_DATABASE_URL`), same as
  `scripts/validate_staging_call_e2e.py`.
* A real FreeSWITCH instance built exactly as
  docs/PHASE-2.25-REAL-SIP-SPOKEN-E2E.md section 2 describes: the same
  `rasonyang/freeswitch-aicc` image Phase 2.24 used, with its `external`
  SIP profile's host port published, its RTP port range published and
  matching `switch.conf.xml`, and this phase's own throwaway
  `01_phase225_e2e.xml` public-context dialplan extension copied in.
* Real Deepgram/OpenAI credentials available through this product's own
  `infra.secrets` (e.g. `SECRETS_ENV_FILE=.env.phase223.local`, the same
  file Phase 2.23's `scripts/validate_staging_ai_pipeline.py` already
  uses) -- never read directly by this script.
* A network path from the FreeSWITCH container to this host for both the
  real media WebSocket (`--media-public-base-url`) and real RTP
  (`--sip-advertise-ip`) -- see this repo's own Phase 2.24 finding re:
  `host.docker.internal`/Docker Desktop's gateway address.

Usage::

    pip install -e ".[staging-sip]"
    ENVIRONMENT=development SECRETS_ENV_FILE=.env.phase223.local \\
    DATABASE_URL=... MIGRATIONS_DATABASE_URL=... REDIS_URL=... \\
    APP_DB_USER=saas_os_app \\
    python scripts/validate_staging_sip_spoken_e2e.py \\
        --sip-host 127.0.0.1 --sip-port 15080 \\
        --media-public-base-url ws://192.168.65.254:8301 \\
        --sip-advertise-ip 192.168.65.254 \\
        --calls 2

Never prints a secret, a full phone number's own carrier routing data
beyond the disposable test E.164 this script itself generates, or more
than a short bounded preview of any transcript.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys as _sys
import threading
import uuid
from pathlib import Path as _Path

import audioop
from core.identity import create_user
from core.tenancy import create_tenant
from pyVoIP.RTP import RTPClient

from voiceagent.agents.config import AgentConfig
from voiceagent.agents.service import create_agent, create_draft_version
from voiceagent.agents.service import publish_version as _publish_version
from voiceagent.calls.service import get_call_session, list_call_sessions
from voiceagent.config.settings import AiProviderSettings
from voiceagent.conversations.service import list_conversation_turns
from voiceagent.phone_numbers.service import register_phone_number
from voiceagent.providers.engines.contracts import FinalTranscript
from voiceagent.providers.stt.registry import create_stt_provider
from voiceagent.providers.tts.registry import create_tts_provider
from voiceagent.runtime.conversation_persistence import ConversationPersistence
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.runtime.fakes import FakeHeartbeatStore
from voiceagent.runtime.heartbeat import RuntimeHeartbeat
from voiceagent.runtime.orchestrator import CallOrchestrator
from voiceagent.runtime.privacy import StaticAiDataPolicySource
from voiceagent.runtime.supervisor import CallRuntime
from voiceagent.runtime.telephony_events import TelephonyEventRouter
from voiceagent.telephony.freeswitch.esl_transport import ManagedEslConnection
from voiceagent.telephony.freeswitch.media import FreeSwitchMediaProvider
from voiceagent.telephony.freeswitch.media_transport import (
    FreeSwitchMediaListener,
    serve_freeswitch_media,
)
from voiceagent.telephony.freeswitch.provider import FreeSwitchTelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.tools.gateway import ToolGateway

_sys.path.insert(0, str(_Path(__file__).parent))
from sip_uac import SipUac  # noqa: E402

_CALLER_UTTERANCE = "Hello, can you tell me what your opening hours are?"
_EXPECTED_REPLY_SUBSTRING = "nine to five"
_AGENT_INSTRUCTIONS = (
    "You are a receptionist for a small clinic. If asked about opening "
    "hours, reply with exactly this sentence and nothing else: "
    "'We are open nine to five, Monday to Friday.' Keep every reply to one "
    "short sentence."
)


def _config() -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "instructions": _AGENT_INSTRUCTIONS,
            "language": "en",
            "voice": {"provider": "deepgram_aura", "voice_id": "aura-asteria-en"},
            "engine": {
                "kind": "pipelined",
                "stt": {"provider": "deepgram", "config": {}},
                "llm": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "config": {"max_tokens": 40, "temperature": 0},
                },
                "tts": {"provider": "deepgram_aura", "config": {}},
            },
            "business_hours": {"timezone": "UTC", "windows": []},
            "privacy": {"data_classification": "tenant_data", "purpose": "conversation"},
        }
    )


def _e164() -> str:
    return f"+1555{uuid.uuid4().int % 10**7:07d}"


def _preview(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


async def _wait_until(predicate, *, timeout_seconds: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


def _pcm16_to_rtp8(pcm16: bytes) -> bytes:
    """16-bit linear PCM -> the 8-bit biased format `pyVoIP.RTP.RTPClient
    .write()` expects (see scripts/sip_uac.py's own docstring)."""
    narrow = audioop.lin2lin(pcm16, 2, 1)
    return audioop.bias(narrow, 1, 128)


def _rtp8_to_pcm16(rtp8: bytes) -> bytes:
    unbiased = audioop.bias(rtp8, 1, -128)
    return audioop.lin2lin(unbiased, 1, 2)


async def _synthesize_caller_line(text: str) -> bytes:
    tts = create_tts_provider("deepgram_aura", {})
    audio = bytearray()
    async for out in tts.synthesize(text, None):
        audio.extend(out.frame)
    return bytes(audio)


async def _transcribe(pcm16: bytes) -> str:
    stt = create_stt_provider("deepgram", {})
    chunk_size = 3200

    async def _frames():
        for i in range(0, len(pcm16), chunk_size):
            yield pcm16[i : i + chunk_size]
            await asyncio.sleep(0.02)

    parts: list[str] = []
    async for event in stt.stream(_frames()):
        if isinstance(event, FinalTranscript) and event.text:
            parts.append(event.text)
    return " ".join(parts).strip()


class _RtpCapture:
    """Continuously drains a real `RTPClient.read()` into a buffer on a
    real background thread, for the lifetime of one real call's real
    media."""

    def __init__(self, rtp: RTPClient) -> None:
        self._rtp = rtp
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            chunk = self._rtp.read(160, blocking=True)
            with self._lock:
                self._buffer.extend(chunk)

    def stop_and_get(self) -> bytes:
        self._stop.set()
        self._thread.join(timeout=2.0)
        with self._lock:
            return bytes(self._buffer)


async def _run_one_call(
    *,
    call_index: int,
    fs_host: str,
    fs_esl_port: int,
    fs_password: str,
    sip_host: str,
    sip_port: int,
    media_public_base_url: str,
    media_listen_host: str,
    media_listen_port: int,
    sip_advertise_ip: str,
    local_sip_port: int,
    local_rtp_port: int,
) -> tuple[int, dict]:
    """Runs one complete real SIP call. Returns (exit_code, evidence)."""
    label = f"call {call_index}"
    evidence: dict = {}

    print(f"[{label}] provisioning a real tenant/agent/phone-number in real PostgreSQL ...")
    tenant = create_tenant(f"phase225-{uuid.uuid4().hex[:8]}")
    user = create_user()
    context = TenantContext(tenant_id=tenant.id, actor_id=user.id, membership_id=uuid.uuid4())
    agent = create_agent(context, name=f"Phase 2.25 Staging Agent {call_index}")
    draft = create_draft_version(context, agent.id, config=_config())
    _publish_version(context, agent.id, draft.id)
    e164 = _e164()
    register_phone_number(context, e164=e164, agent_id=agent.id)
    evidence["tenant_id"] = str(tenant.id)
    evidence["e164"] = e164
    print(f"      tenant={tenant.id} agent={agent.id} phone_number={e164}")

    esl = ManagedEslConnection(
        host=fs_host, port=fs_esl_port, password_provider=lambda: fs_password
    )
    await esl.start()
    media_ticket_secret = "phase225-validation-secret"  # noqa: S105  # pragma: allowlist secret
    telephony = FreeSwitchTelephonyProvider(
        esl,
        media_public_base_url=media_public_base_url,
        media_ticket_secret_provider=lambda: media_ticket_secret,
    )
    media = FreeSwitchMediaProvider()
    media_listener = FreeSwitchMediaListener(media, ticket_secret=media_ticket_secret)
    media_server = await serve_freeswitch_media(
        media_listener, host=media_listen_host, port=media_listen_port
    )
    db = DatabaseBoundary(max_workers=4)
    heartbeats = FakeHeartbeatStore()
    instance_id = f"phase225-staging-runtime-{call_index}"
    await heartbeats.write(
        RuntimeHeartbeat(
            instance_id=instance_id,
            address=f"{instance_id}:0",
            capacity=10,
            current_load=0,
            last_heartbeat_epoch_seconds=0.0,
        ),
        ttl_seconds=60.0,
    )
    call_runtime = CallRuntime(
        instance_id=instance_id, address=f"{instance_id}:0", capacity=10, heartbeat_store=heartbeats
    )
    router = TelephonyEventRouter(telephony)
    policy_source = StaticAiDataPolicySource(
        AiProviderSettings(
            eligible_providers=("deepgram", "openai", "deepgram_aura"),
            allowed_data_classifications=("tenant_data",),
            allowed_purposes=("conversation",),
        )
    )
    orchestrator = CallOrchestrator(
        db=db,
        call_runtime=call_runtime,
        telephony=telephony,
        media=media,
        telephony_events=router,
        heartbeat_store=heartbeats,
        policy_source=policy_source,
        tool_gateway=ToolGateway(),
        conversation_persistence=ConversationPersistence(db),
        system_actor_user_id=user.id,
        system_service_account_name="voiceagent-runtime",
        answer_timeout_seconds=8.0,
    )
    router.on_unrouted_offer = orchestrator.handle_unrouted_offer
    router_task = asyncio.create_task(router.run())

    async def _shutdown() -> None:
        router_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await router_task
        await esl.close()
        db.close()
        media_server.close()
        await media_server.wait_closed()

    print(f"[{label}] synthesizing the caller's real utterance via real Deepgram Aura TTS ...")
    try:
        caller_pcm16 = await _synthesize_caller_line(_CALLER_UTTERANCE)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL ({label}, caller-tts): {exc}")
        await _shutdown()
        return 1, evidence
    caller_audio_seconds = len(caller_pcm16) / 2 / 8000
    print(f"      {len(caller_pcm16)} real PCM bytes ({caller_audio_seconds:.2f}s)")

    uac = SipUac(
        local_ip="0.0.0.0",  # noqa: S104 -- this script's own local test UAC, not a service
        local_sip_port=local_sip_port,
        local_rtp_port=local_rtp_port,
        remote_host=sip_host,
        remote_port=sip_port,
        advertise_ip=sip_advertise_ip,
    )

    invite_result_holder: dict = {}

    def _do_invite() -> None:
        invite_result_holder["result"] = uac.invite(e164, final_timeout=25.0)

    print(f"[{label}] sending a real SIP INVITE to {sip_host}:{sip_port} for {e164} ...")
    invite_thread = threading.Thread(target=_do_invite, daemon=True)
    invite_thread.start()

    print(
        f"[{label}] waiting for the orchestrator to create a real CallSession "
        "from the real OFFERED event ..."
    )
    found_session_id = None

    def _find_session() -> bool:
        nonlocal found_session_id
        rows = list_call_sessions(context, limit=1)
        if rows:
            found_session_id = rows[0].id
            return True
        return False

    ok = await _wait_until(_find_session, timeout_seconds=10.0)
    if not ok:
        print(f"FAIL ({label}): no CallSession was ever created for this real SIP call")
        await _wait_until(lambda: not invite_thread.is_alive(), timeout_seconds=1.0)
        await _shutdown()
        return 1, evidence
    print(f"      real CallSession created: id={found_session_id}")
    evidence["call_session_id"] = str(found_session_id)

    # A blocking `Thread.join()` here would starve this coroutine's own
    # event loop for up to 30 real seconds -- and with it every other task
    # sharing that loop, including `router_task`'s own real ESL command
    # timeouts. Polling with real async sleeps keeps the loop free so the
    # orchestrator's own real work (this script's actual subject under
    # test) can actually run concurrently with this wait.
    await _wait_until(lambda: not invite_thread.is_alive(), timeout_seconds=30.0)
    result = invite_result_holder.get("result")
    if result is None or result.final_status != 200:
        status = result.final_status if result else None
        print(f"FAIL ({label}): real SIP INVITE never received a real 200 OK (got {status!r})")
        with contextlib.suppress(Exception):
            stuck = get_call_session(context, found_session_id)
            await esl.send(f"api uuid_kill {stuck.fs_channel_uuid}")
        await _shutdown()
        return 1, evidence
    print(
        f"      real 200 OK received (provisional={result.provisional_statuses}, "
        f"remote_rtp={result.remote_rtp_ip}:{result.remote_rtp_port})"
    )
    evidence["sip_final_status"] = result.final_status
    evidence["remote_rtp"] = f"{result.remote_rtp_ip}:{result.remote_rtp_port}"

    def _is_media_active() -> bool:
        row = get_call_session(context, found_session_id)
        return row.status in {"in_progress", "completed", "failed", "interrupted"}

    ok = await _wait_until(_is_media_active, timeout_seconds=10.0)
    row = get_call_session(context, found_session_id)
    print(f"      post-answer status: {row.status!r}")
    if row.status != "in_progress":
        print(
            f"FAIL ({label}): call did not reach real in_progress/media-active state "
            f"(status={row.status!r}, end_reason={row.end_reason!r})"
        )
        await _shutdown()
        return 1, evidence
    evidence["reached_in_progress"] = True

    # FreeSWITCH's own SDP answer advertises its RTP endpoint by the
    # container's raw bridge-network IP (e.g. `172.17.0.4`) -- reachable
    # from *inside* the Docker network (confirmed: FreeSWITCH's own outbound
    # RTP to this script arrives fine), but empirically NOT reliably
    # reachable from this host process sending *to* it directly (found via
    # `docker exec ... uuid_record` on a real call: audio sent to that raw
    # IP never arrived -- FreeSWITCH's own recording of the inbound leg was
    # silent the entire time). The container's RTP port range is published
    # 1:1 to the host (`-p 16384-16584:16384-16584/udp`, this phase's own
    # container setup), so routing through the published loopback address
    # instead, same port number, reaches the same real FreeSWITCH socket via
    # Docker's own NAT and is confirmed to work (re-verified: independently
    # re-transcribing a real FreeSWITCH-side recording of audio sent this
    # way reproduced this script's own caller utterance exactly). See
    # docs/PHASE-2.25-REAL-SIP-SPOKEN-E2E.md section 3 for the full
    # investigation.
    rtp = uac.start_rtp(sip_host, result.remote_rtp_port)
    capture = _RtpCapture(rtp)
    capture.start()
    await asyncio.sleep(1.5)

    print(f"[{label}] streaming the real caller utterance into the real RTP session ...")
    caller_rtp8 = _pcm16_to_rtp8(caller_pcm16)
    rtp.write(caller_rtp8)
    await asyncio.sleep(caller_audio_seconds + 1.0)

    print(
        f"[{label}] waiting for the real STT -> LLM -> TTS round trip and real RTP audio back ..."
    )
    await asyncio.sleep(12.0)

    captured_rtp8 = capture.stop_and_get()
    captured_pcm16 = _rtp8_to_pcm16(captured_rtp8)
    print(f"      captured {len(captured_pcm16)} real PCM bytes of return audio")
    evidence["captured_audio_bytes"] = len(captured_pcm16)

    print(
        f"[{label}] independently re-transcribing the real captured return audio "
        "via a fresh real Deepgram STT call ..."
    )
    try:
        heard = await _transcribe(captured_pcm16)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL ({label}, verify-stt): {exc}")
        heard = ""
    print(f"      independently transcribed: {_preview(heard)!r}")
    evidence["independently_transcribed_reply"] = heard
    evidence["tts_audio_reached_caller"] = bool(heard.strip())

    print(
        f"[{label}] checking the real persisted conversation "
        "(proves real STT->LLM ran on this real call) ..."
    )
    turns = await db.run(list_conversation_turns, context, found_session_id)
    user_turns = [t for t in turns if t.role == "user"]
    assistant_turns = [t for t in turns if t.role == "assistant"]
    if user_turns:
        print(
            f"      real transcript (from real caller audio): {_preview(user_turns[0].content)!r}"
        )
    if assistant_turns:
        print(f"      real LLM response: {_preview(assistant_turns[0].content)!r}")
    evidence["real_user_transcript"] = user_turns[0].content if user_turns else None
    evidence["real_assistant_reply"] = assistant_turns[0].content if assistant_turns else None

    print(f"[{label}] caller hangs up (real BYE) ...")
    bye_status = uac.bye(remote_tag=result.remote_tag)
    print(f"      BYE response: {bye_status!r}")
    evidence["bye_status"] = bye_status

    ok = await _wait_until(
        lambda: (
            get_call_session(context, found_session_id).status
            in {"completed", "failed", "interrupted"}
        ),
        timeout_seconds=10.0,
    )
    final_row = get_call_session(context, found_session_id)
    print(
        f"      final CallSession status: {final_row.status!r} end_reason={final_row.end_reason!r}"
    )
    evidence["final_status"] = final_row.status
    evidence["final_end_reason"] = final_row.end_reason

    uac.close()
    await _shutdown()

    if not ok:
        print(f"FAIL ({label}): CallSession never reached a terminal status after real caller BYE")
        return 1, evidence

    real_transcript_ok = bool(
        user_turns
        and "opening hours" in user_turns[0].content.lower()
        and assistant_turns
        and _EXPECTED_REPLY_SUBSTRING in assistant_turns[0].content.lower()
    )
    evidence["real_stt_llm_confirmed"] = real_transcript_ok
    audio_returned_ok = _EXPECTED_REPLY_SUBSTRING in heard.lower()

    stt_llm_word = "confirmed" if real_transcript_ok else "NOT CONFIRMED"
    audio_label = "GREEN" if audio_returned_ok else "NOT VALIDATED"
    audio_word = (
        "confirmed"
        if audio_returned_ok
        else (
            "not observed (see docs/PHASE-2.25-REAL-SIP-SPOKEN-E2E.md section 3 -- "
            "suspected vendor mod_audio_stream playback limitation)"
        )
    )
    print("      GREEN  -- real SIP signaling (200 OK over a real INVITE): confirmed")
    print("      GREEN  -- real RTP + real FreeSWITCH media attach (in_progress): confirmed")
    print(f"      GREEN  -- real caller audio -> real STT transcript: {stt_llm_word}")
    print(f"      GREEN  -- real LLM deterministic response persisted: {stt_llm_word}")
    print(f"      {audio_label} -- real TTS audio reaching the caller over real RTP: {audio_word}")
    print("      GREEN  -- real caller hangup -> real terminal CallSession status: confirmed")

    if not real_transcript_ok:
        print(f"FAIL ({label}): real STT/LLM round trip over the real SIP call was not confirmed")
        return 1, evidence

    if not audio_returned_ok:
        print(
            f"PARTIAL ({label}): real spoken input -> real STT -> real LLM confirmed end to end "
            f"over a genuine SIP/RTP call; real TTS audio return-to-caller NOT VALIDATED "
            f"(see docs/PHASE-2.25-REAL-SIP-SPOKEN-E2E.md)"
        )
        return 2, evidence

    print(f"PASS ({label}): complete real spoken SIP round trip confirmed, both directions")
    return 0, evidence


async def _run(args: argparse.Namespace) -> int:
    overall = 0
    all_evidence = []
    for i in range(1, args.calls + 1):
        media_listen_port = args.media_listen_port + i
        # The port this call's own listener actually binds and the port in
        # the URL FreeSWITCH is told to connect to (`media_public_base_url`)
        # must be the exact same one -- offsetting only the former across
        # sequential calls while leaving the latter fixed silently points
        # FreeSWITCH's own `mod_audio_stream` at a port nothing is
        # listening on for every call after the first, which it reports
        # only as an unhelpful, instantaneous "connection error" with no
        # further detail (found the hard way -- see
        # docs/PHASE-2.25-REAL-SIP-SPOKEN-E2E.md section 3).
        base_url, _, base_port_str = args.media_public_base_url.rpartition(":")
        per_call_media_url = f"{base_url}:{int(base_port_str) + i}"
        code, evidence = await _run_one_call(
            call_index=i,
            fs_host=args.fs_host,
            fs_esl_port=args.fs_esl_port,
            fs_password=args.fs_password,
            sip_host=args.sip_host,
            sip_port=args.sip_port,
            media_public_base_url=per_call_media_url,
            media_listen_host=args.media_listen_host,
            media_listen_port=media_listen_port,
            sip_advertise_ip=args.sip_advertise_ip,
            local_sip_port=args.local_sip_port + i,
            local_rtp_port=args.local_rtp_port + i,
        )
        all_evidence.append(evidence)
        # Exit code 1 is a hard failure (signaling/lifecycle/STT-LLM never
        # confirmed) -- stop. Exit code 2 is the known, documented partial
        # result (real STT->LLM confirmed, real TTS audio return not
        # observed) -- still worth attempting the remaining calls for
        # cross-call isolation evidence.
        if code == 1:
            overall = 1
            break
        overall = max(overall, code)

    if len(all_evidence) >= 2:
        tenants = {e.get("tenant_id") for e in all_evidence}
        sessions = {e.get("call_session_id") for e in all_evidence}
        print(
            f"[cross-call] distinct tenants: {len(tenants)}, distinct CallSessions: {len(sessions)}"
        )
        if len(tenants) != len(all_evidence) or len(sessions) != len(all_evidence):
            print("FAIL: cross-call isolation check found reused tenant/CallSession identity")
            overall = 1

    if overall == 0:
        print("PASS: overall -- complete real spoken SIP round trip, both directions")
    elif overall == 2:
        print(
            "PARTIAL: overall -- real SIP/RTP/STT/LLM round trip confirmed; "
            "real TTS audio return-to-caller NOT VALIDATED "
            "(see docs/PHASE-2.25-REAL-SIP-SPOKEN-E2E.md)"
        )
    else:
        print("FAIL: overall")
    return overall


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fs-host", default="127.0.0.1")
    parser.add_argument("--fs-esl-port", type=int, default=18023)
    parser.add_argument("--fs-password", default="ClueCon")
    parser.add_argument("--sip-host", default="127.0.0.1")
    parser.add_argument("--sip-port", type=int, default=15080)
    parser.add_argument("--media-public-base-url", required=True)
    parser.add_argument("--media-listen-host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--media-listen-port", type=int, default=8310)
    parser.add_argument("--sip-advertise-ip", required=True)
    parser.add_argument("--local-sip-port", type=int, default=15070)
    parser.add_argument("--local-rtp-port", type=int, default=15170)
    parser.add_argument("--calls", type=int, default=1)
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
