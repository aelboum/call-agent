#!/usr/bin/env python
"""Phase 2.27: real concurrent-call media quality & resource validation.

Phase 2.26's own `--concurrent` mode (`scripts/validate_staging_sip_spoken_
e2e.py`) gave every simultaneous call its own `ManagedEslConnection`, its
own `FreeSwitchMediaProvider`/media listener port, and its own
`CallRuntime` -- fully isolated infrastructure per call. That is *not* how
one real `call-runtime` process actually works: `voiceagent.runtime
.orchestrator`'s own module docstring is explicit that "one `CallOrchestrator`
per process" reacts to "the identical broadcast FreeSWITCH event stream its
own `ManagedEslConnection`" -- one shared ESL connection (with its own
serializing `_send_lock`,
`voiceagent.telephony.freeswitch.esl_transport.EslTcpConnection`), and one
shared media listener (`voiceagent.telephony.freeswitch.media_transport
.FreeSwitchMediaListener` -- already ticket-routed to multiplex any number
of calls over one port), handle every concurrent call that one process
owns. Phase 2.26's own concurrent test therefore could not have exercised
the one shared-resource path (the ESL connection's own command
serialization) production concurrency actually goes through -- this script
does.

**What this adds over Phase 2.26's own concurrent mode:**

1. One shared `ManagedEslConnection`, `FreeSwitchTelephonyProvider`,
   `FreeSwitchMediaProvider`, media listener (one port), `CallRuntime`,
   `DatabaseBoundary`, and `CallOrchestrator` for every simultaneous call --
   the actual production shape.
2. Distinct, deterministic response phrases per call (brief section 7) so
   cross-call audio contamination is mechanically detectable, not merely
   assumed absent.
3. Fixture provisioning (tenant/agent/phone-number -- real synchronous
   SaaS-OS calls, never wrapped in `DatabaseBoundary.run()`, because no
   production call path ever provisions these live) for every call happens
   *before* any simultaneous SIP traffic starts, specifically so this
   script's own setup-phase blocking (a test-harness property, never a
   production one -- `CallOrchestrator` uses `await self._db.run(...)`
   uniformly, confirmed by reading it) cannot masquerade as a concurrency
   finding about the product's own call-handling path.
4. Lightweight resource sampling: this process's own CPU time
   (`time.process_time()`, stdlib, no new dependency) and the real
   FreeSWITCH container's own CPU/memory (`docker stats`, external
   process, no new dependency) during the concurrent window.
5. Precise per-call timeline timestamps (invite sent, session created,
   in_progress, caller audio streamed, capture stopped, terminal state) for
   correlating against FreeSWITCH's own `mod_audio_stream::play`/
   `uuid_broadcast` log timestamps independently.

Reuses `scripts/sip_uac.py` and `scripts/validate_staging_sip_spoken_e2e
.py`'s own helpers unchanged (`_pcm16_to_rtp8`, `_rtp8_to_pcm16`,
`_synthesize_caller_line`, `_transcribe`, `_RtpCapture`, `_wait_until`,
`_preview`) rather than re-implementing them -- this script's only new
ingredient is the shared-infra wiring and the instrumentation above.

Staging-only test tooling, not product code, not imported by anything
under `voiceagent/`. Never prints a secret or more than a short bounded
transcript preview.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys as _sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path as _Path

from core.identity import create_user
from core.tenancy import create_tenant

from voiceagent.agents.config import AgentConfig
from voiceagent.agents.service import create_agent, create_draft_version
from voiceagent.agents.service import publish_version as _publish_version
from voiceagent.calls.service import get_call_session, list_call_sessions
from voiceagent.config.settings import AiProviderSettings
from voiceagent.conversations.service import list_conversation_turns
from voiceagent.phone_numbers.service import register_phone_number
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
from validate_staging_sip_spoken_e2e import (  # noqa: E402
    _pcm16_to_rtp8,
    _preview,
    _rtp8_to_pcm16,
    _RtpCapture,
    _synthesize_caller_line,
    _transcribe,
    _wait_until,
)

#: Deterministic, distinct (question, unique-reply-sentence, distinguishing
#: keyword) triples -- brief section 7: "use different deterministic
#: response phrases per call so cross-call contamination becomes
#: detectable." Test-tooling only, never hardcoded into production logic
#: (`voiceagent/` never sees these strings). Verification checks for the
#: single distinguishing NATO-alphabet `keyword`, not the full sentence:
#: real Deepgram STT over a synthesized-then-8kHz-recompressed-then-
#: re-transcribed round trip is never word-perfect (e.g. "ALPHA ONE NINER"
#: legitimately came back as "alpha one nine" in testing) -- the keyword
#: alone is what must be present for this call and absent for every other,
#: which is the actual cross-contamination question, without being
#: defeated by ordinary STT noise unrelated to it.
_CALL_PHRASES = [
    ("What is today's code word?", "The code word is ALPHA ONE NINER", "alpha"),
    ("What is today's code word?", "The code word is BRAVO TWO FOXTROT", "bravo"),
    ("What is today's code word?", "The code word is CHARLIE THREE TANGO", "charlie"),
    ("What is today's code word?", "The code word is DELTA FOUR WHISKEY", "delta"),
]


def _agent_instructions(reply: str) -> str:
    return (
        "You are a verification bot. If asked for the code word, reply "
        f"with exactly this sentence and nothing else: '{reply}.' Keep "
        "every reply to one short sentence."
    )


def _config(reply: str) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "instructions": _agent_instructions(reply),
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


@dataclass
class ResourceSample:
    at: float
    fs_cpu_percent: float | None
    fs_mem_usage: str | None


class FreeSwitchResourceMonitor:
    """Samples the real FreeSWITCH container's own `docker stats` at a
    fixed interval for the lifetime of the concurrent window -- external
    process, no new Python dependency (brief section 10: only useful
    telemetry, no raw audio/content, nothing tenant-identifying)."""

    def __init__(self, container: str, interval_seconds: float = 1.0) -> None:
        self._container = container
        self._interval = interval_seconds
        self.samples: list[ResourceSample] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _sample_once(self) -> ResourceSample:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.CPUPerc}}\t{{.MemUsage}}",
                self._container,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
        except OSError:
            return ResourceSample(at=time.monotonic(), fs_cpu_percent=None, fs_mem_usage=None)
        text = out.decode("utf-8", errors="replace").strip()
        if not text:
            return ResourceSample(at=time.monotonic(), fs_cpu_percent=None, fs_mem_usage=None)
        cpu_str, _, mem = text.partition("\t")
        cpu: float | None
        try:
            cpu = float(cpu_str.rstrip("%"))
        except ValueError:
            cpu = None
        return ResourceSample(at=time.monotonic(), fs_cpu_percent=cpu, fs_mem_usage=mem or None)

    async def _run(self) -> None:
        while not self._stop.is_set():
            self.samples.append(await self._sample_once())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> list[ResourceSample]:
        self._stop.set()
        if self._task is not None:
            await self._task
        return self.samples

    def summary(self) -> str:
        cpu_values = [s.fs_cpu_percent for s in self.samples if s.fs_cpu_percent is not None]
        if not cpu_values:
            return "no samples captured"
        return (
            f"n={len(cpu_values)} min={min(cpu_values):.1f}% "
            f"avg={sum(cpu_values) / len(cpu_values):.1f}% max={max(cpu_values):.1f}%"
        )


@dataclass
class CallFixture:
    call_index: int
    tenant_id: uuid.UUID
    context: TenantContext
    e164: str
    caller_question: str
    expected_reply: str
    keyword: str


@dataclass
class CallTimeline:
    call_index: int
    invite_sent_at: float = 0.0
    session_created_at: float = 0.0
    in_progress_at: float = 0.0
    caller_stream_started_at: float = 0.0
    caller_stream_ended_at: float = 0.0
    ai_wait_ended_at: float = 0.0
    capture_stopped_at: float = 0.0
    bye_at: float = 0.0
    terminal_at: float = 0.0

    def as_dict(self) -> dict:
        return {k: round(v, 3) for k, v in vars(self).items() if k != "call_index"}


def _provision_fixture(call_index: int, user_id: uuid.UUID) -> CallFixture:
    """Real SaaS-OS/product fixture creation -- deliberately run *before*
    any simultaneous SIP traffic starts (see this module's own docstring,
    point 3): these are bare, unwrapped synchronous calls (matching Phase
    2.25/2.26's own script), which would block the shared event loop for
    every other concurrent call in flight if run during the live-call
    phase. No production call path ever provisions a tenant/agent/phone
    number live during a call, so this ordering isolates the concurrency
    question this phase actually asks (does *concurrent call handling*
    degrade audio) from an artifact of this script's own setup code."""
    question, reply, keyword = _CALL_PHRASES[(call_index - 1) % len(_CALL_PHRASES)]
    tenant = create_tenant(f"phase227-{uuid.uuid4().hex[:8]}")
    context = TenantContext(tenant_id=tenant.id, actor_id=user_id, membership_id=uuid.uuid4())
    agent = create_agent(context, name=f"Phase 2.27 Verification Bot {call_index}")
    draft = create_draft_version(context, agent.id, config=_config(reply))
    _publish_version(context, agent.id, draft.id)
    e164 = _e164()
    register_phone_number(context, e164=e164, agent_id=agent.id)
    return CallFixture(
        call_index=call_index,
        tenant_id=tenant.id,
        context=context,
        e164=e164,
        caller_question=question,
        expected_reply=reply,
        keyword=keyword,
    )


async def _run_call_on_shared_infra(
    fixture: CallFixture,
    *,
    db: DatabaseBoundary,
    sip_host: str,
    sip_port: int,
    sip_advertise_ip: str,
    local_sip_port: int,
    local_rtp_port: int,
) -> tuple[int, dict]:
    """One call's own live-traffic scenario against infra that is already
    running and shared with every other concurrent call -- no
    construction/teardown of telephony/media/orchestrator here (see
    `_run_shared()`, which owns that for the whole batch)."""
    label = f"call {fixture.call_index}"
    evidence: dict = {"tenant_id": str(fixture.tenant_id), "e164": fixture.e164}
    timeline = CallTimeline(call_index=fixture.call_index)
    context = fixture.context

    print(f"[{label}] synthesizing this call's own caller utterance via real Deepgram Aura TTS ...")
    try:
        caller_pcm16 = await _synthesize_caller_line(fixture.caller_question)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL ({label}, caller-tts): {exc}")
        return 1, evidence
    caller_audio_seconds = len(caller_pcm16) / 2 / 8000

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
        invite_result_holder["result"] = uac.invite(fixture.e164, final_timeout=25.0)

    timeline.invite_sent_at = time.monotonic()
    print(f"[{label}] sending a real SIP INVITE for {fixture.e164} ...")
    invite_thread = threading.Thread(target=_do_invite, daemon=True)
    invite_thread.start()

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
        return 1, evidence
    timeline.session_created_at = time.monotonic()
    evidence["call_session_id"] = str(found_session_id)

    await _wait_until(lambda: not invite_thread.is_alive(), timeout_seconds=30.0)
    result = invite_result_holder.get("result")
    if result is None or result.final_status != 200:
        status = result.final_status if result else None
        print(f"FAIL ({label}): real SIP INVITE never received a real 200 OK (got {status!r})")
        return 1, evidence
    evidence["sip_call_id"] = result.call_id
    evidence["sip_final_status"] = result.final_status

    def _is_media_active() -> bool:
        row = get_call_session(context, found_session_id)
        return row.status in {"in_progress", "completed", "failed", "interrupted"}

    ok = await _wait_until(_is_media_active, timeout_seconds=10.0)
    row = get_call_session(context, found_session_id)
    evidence["fs_channel_uuid"] = row.fs_channel_uuid
    if row.status != "in_progress":
        print(
            f"FAIL ({label}): call did not reach real in_progress/media-active state "
            f"(status={row.status!r}, end_reason={row.end_reason!r})"
        )
        return 1, evidence
    timeline.in_progress_at = time.monotonic()
    print(
        f"      {label}: real CallSession={found_session_id} fs_uuid={row.fs_channel_uuid} "
        f"sip_call_id={result.call_id} -- in_progress"
    )

    rtp = uac.start_rtp(sip_host, result.remote_rtp_port)
    capture = _RtpCapture(rtp)
    capture.start()
    await asyncio.sleep(1.5)

    timeline.caller_stream_started_at = time.monotonic()
    caller_rtp8 = _pcm16_to_rtp8(caller_pcm16)
    rtp.write(caller_rtp8)
    await asyncio.sleep(caller_audio_seconds + 1.0)
    timeline.caller_stream_ended_at = time.monotonic()

    await asyncio.sleep(12.0)
    timeline.ai_wait_ended_at = time.monotonic()

    captured_rtp8 = capture.stop_and_get()
    timeline.capture_stopped_at = time.monotonic()
    captured_pcm16 = _rtp8_to_pcm16(captured_rtp8)
    evidence["captured_audio_bytes"] = len(captured_pcm16)
    evidence["captured_audio_seconds"] = len(captured_pcm16) / 2 / 8000

    try:
        heard = await _transcribe(captured_pcm16)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL ({label}, verify-stt): {exc}")
        heard = ""
    print(f"      {label}: independently transcribed return audio: {_preview(heard)!r}")
    evidence["independently_transcribed_reply"] = heard

    turns = await db.run(list_conversation_turns, context, found_session_id)
    user_turns = [t for t in turns if t.role == "user"]
    assistant_turns = [t for t in turns if t.role == "assistant"]
    evidence["real_user_transcript"] = user_turns[0].content if user_turns else None
    evidence["real_assistant_reply"] = assistant_turns[0].content if assistant_turns else None
    print(f"      {label}: real LLM response: {_preview(evidence['real_assistant_reply'] or '')!r}")

    timeline.bye_at = time.monotonic()
    bye_status = uac.bye(remote_tag=result.remote_tag)
    evidence["bye_status"] = bye_status

    ok = await _wait_until(
        lambda: (
            get_call_session(context, found_session_id).status
            in {"completed", "failed", "interrupted"}
        ),
        timeout_seconds=10.0,
    )
    timeline.terminal_at = time.monotonic()
    final_row = get_call_session(context, found_session_id)
    evidence["final_status"] = final_row.status
    evidence["final_end_reason"] = final_row.end_reason
    print(
        f"      {label}: final CallSession status={final_row.status!r} "
        f"end_reason={final_row.end_reason!r}"
    )

    uac.close()

    if not ok:
        print(f"FAIL ({label}): CallSession never reached a terminal status after real caller BYE")
        return 1, evidence

    # --- audio-quality verification (brief section 7) --------------------
    # Keyword-based, not full-sentence: real Deepgram STT over a
    # synthesized -> 8kHz PCMU -> re-transcribed round trip is never
    # word-perfect (found empirically: "ALPHA ONE NINER" legitimately came
    # back as "alpha one nine") -- the single distinguishing NATO keyword is
    # what actually answers the cross-contamination question.
    heard_lower = heard.lower()
    own_reply_present = fixture.keyword in heard_lower
    other_keywords_present = [
        other_keyword
        for _, _, other_keyword in _CALL_PHRASES
        if other_keyword != fixture.keyword and other_keyword in heard_lower
    ]
    evidence["own_reply_present"] = own_reply_present
    evidence["cross_contamination_detected"] = bool(other_keywords_present)
    evidence["timeline"] = timeline.as_dict()

    duration_ok = evidence["captured_audio_seconds"] >= 0.2
    non_silent_ok = evidence["captured_audio_bytes"] > 0

    if other_keywords_present:
        print(
            f"FAIL ({label}): cross-call contamination -- heard another call's own "
            f"keyword: {other_keywords_present!r}"
        )
        return 1, evidence

    if not (own_reply_present and duration_ok and non_silent_ok):
        print(
            f"PARTIAL ({label}): real spoken input -> real STT -> real LLM round trip observed, "
            f"but this call's own return audio was not verified intelligible "
            f"(own_reply_present={own_reply_present}, bytes={evidence['captured_audio_bytes']})"
        )
        return 2, evidence

    print(
        f"PASS ({label}): real spoken round trip confirmed, own reply verified, "
        "no cross-contamination"
    )
    return 0, evidence


async def _run_shared(args: argparse.Namespace) -> int:
    print(
        f"[setup] provisioning {args.calls} real tenant/agent/phone-number fixtures "
        "(sequential, before any simultaneous SIP traffic) ..."
    )
    user = create_user()
    fixtures = [_provision_fixture(i, user.id) for i in range(1, args.calls + 1)]
    for f in fixtures:
        print(
            f"      call {f.call_index}: tenant={f.tenant_id} phone={f.e164} "
            f"own_reply={f.expected_reply!r}"
        )

    esl = ManagedEslConnection(
        host=args.fs_host, port=args.fs_esl_port, password_provider=lambda: args.fs_password
    )
    await esl.start()
    media_ticket_secret = "phase227-validation-secret"  # noqa: S105  # pragma: allowlist secret
    telephony = FreeSwitchTelephonyProvider(
        esl,
        media_public_base_url=args.media_public_base_url,
        media_ticket_secret_provider=lambda: media_ticket_secret,
    )
    media = FreeSwitchMediaProvider()
    media_listener = FreeSwitchMediaListener(media, ticket_secret=media_ticket_secret)
    media_server = await serve_freeswitch_media(
        media_listener, host=args.media_listen_host, port=args.media_listen_port
    )
    db = DatabaseBoundary(max_workers=max(4, args.calls * 2))
    heartbeats = FakeHeartbeatStore()
    instance_id = "phase227-shared-staging-runtime"
    await heartbeats.write(
        RuntimeHeartbeat(
            instance_id=instance_id,
            address=f"{instance_id}:0",
            capacity=max(10, args.calls),
            current_load=0,
            last_heartbeat_epoch_seconds=0.0,
        ),
        ttl_seconds=60.0,
    )
    call_runtime = CallRuntime(
        instance_id=instance_id,
        address=f"{instance_id}:0",
        capacity=max(10, args.calls),
        heartbeat_store=heartbeats,
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

    monitor = (
        FreeSwitchResourceMonitor(args.fs_container, interval_seconds=1.0)
        if args.fs_container
        else None
    )
    if monitor:
        monitor.start()

    cpu_time_start = time.process_time()
    wall_start = time.monotonic()

    print(
        f"[shared-infra] {args.calls} call(s) sharing ONE ESL connection, ONE media listener "
        f"(port {args.media_listen_port}), ONE CallRuntime -- launching simultaneously ..."
    )
    try:
        results = await asyncio.gather(
            *(
                _run_call_on_shared_infra(
                    f,
                    db=db,
                    sip_host=args.sip_host,
                    sip_port=args.sip_port,
                    sip_advertise_ip=args.sip_advertise_ip,
                    local_sip_port=args.local_sip_port + f.call_index,
                    local_rtp_port=args.local_rtp_port + f.call_index,
                )
                for f in fixtures
            )
        )
    finally:
        cpu_time_end = time.process_time()
        wall_end = time.monotonic()
        router_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await router_task
        await esl.close()
        db.close()
        media_server.close()
        await media_server.wait_closed()
        if monitor:
            await monitor.stop()

    wall_elapsed = wall_end - wall_start
    cpu_elapsed = cpu_time_end - cpu_time_start
    print(
        f"[resources] this validation process: wall={wall_elapsed:.1f}s "
        f"cpu={cpu_elapsed:.1f}s (cpu/wall={cpu_elapsed / wall_elapsed:.2f})"
    )
    if monitor:
        print(
            f"[resources] real FreeSWITCH container ({args.fs_container}) CPU: {monitor.summary()}"
        )

    overall = 0
    all_evidence = []
    for code, evidence in results:
        all_evidence.append(evidence)
        overall = 1 if code == 1 else max(overall, code)

    tenants = {e.get("tenant_id") for e in all_evidence}
    sessions = {e.get("call_session_id") for e in all_evidence}
    fs_uuids = {e.get("fs_channel_uuid") for e in all_evidence}
    print(
        f"[cross-call] distinct tenants={len(tenants)} distinct CallSessions={len(sessions)} "
        f"distinct fs_channel_uuid={len(fs_uuids)} (of {len(all_evidence)} calls)"
    )
    if len({len(all_evidence), len(tenants), len(sessions), len(fs_uuids)}) != 1:
        print("FAIL: isolation check found reused tenant/CallSession/fs_channel_uuid identity")
        overall = 1

    contaminated = [e for e in all_evidence if e.get("cross_contamination_detected")]
    if contaminated:
        print(f"FAIL: {len(contaminated)} call(s) heard another call's own reply phrase")
        overall = 1

    audio_seconds = [e.get("captured_audio_seconds", 0.0) for e in all_evidence]
    own_reply_flags = [e.get("own_reply_present", False) for e in all_evidence]
    print(f"[audio] captured seconds per call: {[round(s, 2) for s in audio_seconds]}")
    print(f"[audio] own-reply-verified per call: {own_reply_flags}")

    if overall == 0:
        print(
            f"PASS: overall -- {args.calls} concurrent call(s), own reply verified, "
            "no cross-contamination"
        )
    elif overall == 2:
        print(
            f"PARTIAL: overall -- {args.calls} concurrent call(s), "
            "some return audio not fully verified"
        )
    else:
        print("FAIL: overall")
    return overall


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fs-host", default="127.0.0.1")
    parser.add_argument("--fs-esl-port", type=int, default=18023)
    parser.add_argument("--fs-password", default="ClueCon")
    parser.add_argument(
        "--fs-container",
        default="",
        help="docker container name for CPU/mem sampling; empty disables sampling",
    )
    parser.add_argument("--sip-host", default="127.0.0.1")
    parser.add_argument("--sip-port", type=int, default=15080)
    parser.add_argument("--media-public-base-url", required=True)
    parser.add_argument("--media-listen-host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--media-listen-port", type=int, default=8600)
    parser.add_argument("--sip-advertise-ip", required=True)
    parser.add_argument("--local-sip-port", type=int, default=15700)
    parser.add_argument("--local-rtp-port", type=int, default=15800)
    parser.add_argument("--calls", type=int, default=2)
    args = parser.parse_args()
    return asyncio.run(_run_shared(args))


if __name__ == "__main__":
    raise SystemExit(main())
