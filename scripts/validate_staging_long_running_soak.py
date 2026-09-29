#!/usr/bin/env python
"""Phase 2.30: real long-running stability & repeated-failure cleanup
validation.

Phase 2.27/2.28/2.29's own `scripts/validate_staging_concurrent_media_e2e
.py` already builds ONE shared `ManagedEslConnection`, ONE shared media
listener, ONE shared `CallRuntime`/`DatabaseBoundary`/`CallOrchestrator` for
a batch of calls -- but its own `_run_shared()` launches every call in that
batch *simultaneously* via `asyncio.gather()` and then exits the process.
That is the right shape for a concurrency question; it is the wrong shape
for this phase's own question, which is: does the SAME long-lived
runtime/process remain healthy across many SUCCESSIVE calls, including
repeated induced failures, without a process restart between any of them?

This script reuses that module's own fixture/call helpers unchanged
(`_provision_fixture`, `_run_call_on_shared_infra`, `CallFixture`,
`FreeSwitchResourceMonitor`, `_CALL_PHRASES` -- imported, never
copy-pasted) and its own shared-infra construction (duplicated here only
because `_run_shared()` bundles setup and the concurrent-gather execution
together as one function with no seam to call setup alone), then drives
calls through that shared infra ONE AT A TIME, in a caller-supplied
sequence of `normal` / `media_fail` / `cancel` (a real early caller hangup
mid-AI-processing, `--early-hangup-seconds`, the same real-staging analog
Phase 2.28 already established and documented for exercising a live
hangup-during-processing scenario against the real stack -- genuine
`CallRuntime.cancel_call()`-initiated cancellation mid-processing is
already covered hermetically, see
`tests/integration/test_runtime_integration.py
::test_hangup_mid_stalled_ai_turn_leaves_a_concurrent_call_unaffected`;
this script does not attempt to re-exercise that exact supervisor-side
trigger against the real SIP stack, since `_run_call_on_shared_infra` has
no hook for it without modifying that shared helper -- an explicit,
documented limitation, not a silent gap), while sampling resources at
caller-chosen checkpoints.

Resource sampling, all read-only, no new production dependency:
* this process's own Working Set (RSS) via `ctypes`/`psapi.dll`
  (`GetProcessMemoryInfo` against `GetCurrentProcess()`) -- Windows-only,
  matching this repository's own dev/staging platform; no `psutil`
  dependency added.
* `len(asyncio.all_tasks())` -- total live asyncio tasks in this process.
* `CallRuntime.current_load` (`len(self._tasks)`) -- the runtime's own
  count of calls it believes are still active; expected to return to 0
  between calls in a sequential soak.
* `len(CallRuntime._errors)` -- Phase 2.30's own headline finding (see
  `docs/PHASE-2.30-LONG-RUNNING-STABILITY.md` section 1): this dict is
  populated on every call that raises out of `run_call_task()` (including
  every `TransportError`-classified media disconnect, Phase 2.29's own
  fix) and is only ever removed by `CallRuntime.start_call()` popping the
  *same* `call_session_id` before a restart -- which never happens for a
  real call, since every real call gets a fresh UUID. Reading this private
  attribute is read-only introspection for validation evidence, not a
  production code change.
* `len(FreeSwitchMediaProvider._streams)` /
  `len(FreeSwitchMediaProvider._sockets)` -- active media stream/socket
  bookkeeping; expected to return to 0 between calls.
* `threading.active_count()` and `len(DatabaseBoundary._executor._threads)`
  -- coarse proxies for the DB executor's own thread pool behavior (private
  attribute access, read-only, validation evidence only).
* the real FreeSWITCH container's own CPU/mem via `docker stats
  --no-stream <container>` (subprocess, no new dependency).

Staging-only test tooling, not product code, not imported by anything
under `voiceagent/`. Never prints a secret or more than a short bounded
transcript preview.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import json
import subprocess
import sys as _sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path as _Path

from core.identity import create_user

from voiceagent.config.settings import AiProviderSettings
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
from voiceagent.tools.gateway import ToolGateway

_sys.path.insert(0, str(_Path(__file__).parent))
from validate_staging_concurrent_media_e2e import (  # noqa: E402
    CallFixture,
    _provision_fixture,
    _run_call_on_shared_infra,
)


def _rss_bytes() -> int:
    """This process's own Working Set Size, via `psapi.dll`. Windows-only
    (matches this repository's own dev/staging platform) -- no new
    dependency (`ctypes` is stdlib)."""

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("PageFaultCount", ctypes.c_uint32),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.c_uint32,
    ]
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    ok = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    )
    if not ok:
        return -1
    return counters.WorkingSetSize


def _docker_stats(container: str) -> str:
    try:
        out = subprocess.run(  # noqa: S603 -- fixed, local, validation-only invocation
            [  # noqa: S607 -- fixed, local, validation-only invocation
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.CPUPerc}} {{.MemUsage}}",
                container,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.strip() or f"(no output, stderr={out.stderr.strip()!r})"
    except Exception as exc:  # noqa: BLE001 -- sampling must never abort the soak
        return f"(sampling failed: {exc})"


@dataclass
class ResourceCheckpoint:
    label: str
    calls_completed: int
    rss_bytes: int
    asyncio_task_count: int
    call_runtime_current_load: int
    call_runtime_error_dict_size: int
    call_runtime_cancellation_dict_size: int
    media_active_streams: int
    media_active_sockets: int
    threading_active_count: int
    db_executor_thread_count: int
    fs_docker_stats: str
    wall_elapsed_seconds: float


@dataclass
class CallOutcome:
    call_index: int
    scenario: str
    tenant_id: str
    call_session_id: str | None
    fs_channel_uuid: str | None
    sip_call_id: str | None
    final_status: str | None
    final_end_reason: str | None
    own_reply_present: bool | None
    cross_contamination_detected: bool | None
    error_dict_size_after: int
    runtime_load_after: int
    result_code: int
    raw_evidence: dict = field(default_factory=dict)


def _sample(
    label: str,
    *,
    calls_completed: int,
    call_runtime: CallRuntime,
    media: FreeSwitchMediaProvider,
    db: DatabaseBoundary,
    fs_container: str,
    wall_start: float,
) -> ResourceCheckpoint:
    cp = ResourceCheckpoint(
        label=label,
        calls_completed=calls_completed,
        rss_bytes=_rss_bytes(),
        asyncio_task_count=len(asyncio.all_tasks()),
        call_runtime_current_load=call_runtime.current_load,
        call_runtime_error_dict_size=len(call_runtime._errors),  # noqa: SLF001 -- read-only validation introspection
        call_runtime_cancellation_dict_size=len(call_runtime._cancellations),  # noqa: SLF001
        media_active_streams=len(media._streams),  # noqa: SLF001
        media_active_sockets=len(media._sockets),  # noqa: SLF001
        threading_active_count=threading.active_count(),
        db_executor_thread_count=len(db._executor._threads),  # noqa: SLF001
        fs_docker_stats=_docker_stats(fs_container) if fs_container else "(disabled)",
        wall_elapsed_seconds=round(time.monotonic() - wall_start, 1),
    )
    print(
        f"[resource-checkpoint:{label}] calls={calls_completed} "
        f"rss={cp.rss_bytes / (1024 * 1024):.1f}MiB "
        f"asyncio_tasks={cp.asyncio_task_count} "
        f"runtime_load={cp.call_runtime_current_load} "
        f"runtime_errors_dict_size={cp.call_runtime_error_dict_size} "
        f"runtime_cancellations_dict_size={cp.call_runtime_cancellation_dict_size} "
        f"media_streams={cp.media_active_streams} media_sockets={cp.media_active_sockets} "
        f"threads={cp.threading_active_count} db_executor_threads={cp.db_executor_thread_count} "
        f"fs_stats={cp.fs_docker_stats!r} wall={cp.wall_elapsed_seconds}s"
    )
    return cp


async def _run_soak(args: argparse.Namespace) -> int:
    scenarios = [s.strip() for s in args.sequence.split(",") if s.strip()]
    if not scenarios:
        raise ValueError("--sequence must contain at least one scenario")
    for s in scenarios:
        if s not in {"normal", "media_fail", "cancel"}:
            raise ValueError(f"unknown scenario {s!r} (expected normal|media_fail|cancel)")

    print(f"[setup] provisioning {len(scenarios)} real tenant/agent/phone-number fixtures ...")
    user = create_user()
    fixtures: list[CallFixture] = [
        _provision_fixture(i, user.id, phrase_mode="distinct") for i in range(1, len(scenarios) + 1)
    ]

    esl = ManagedEslConnection(
        host=args.fs_host, port=args.fs_esl_port, password_provider=lambda: args.fs_password
    )
    await esl.start()
    media_ticket_secret = "phase230-validation-secret"  # noqa: S105  # pragma: allowlist secret
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
    db = DatabaseBoundary(max_workers=8)
    heartbeats = FakeHeartbeatStore()
    instance_id = "phase230-long-running-soak-runtime"
    await heartbeats.write(
        RuntimeHeartbeat(
            instance_id=instance_id,
            address=f"{instance_id}:0",
            capacity=20,
            current_load=0,
            last_heartbeat_epoch_seconds=0.0,
        ),
        ttl_seconds=3600.0,
    )
    call_runtime = CallRuntime(
        instance_id=instance_id, address=f"{instance_id}:0", capacity=20, heartbeat_store=heartbeats
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

    wall_start = time.monotonic()
    outcomes: list[CallOutcome] = []
    checkpoints: list[ResourceCheckpoint] = []

    def sample(label: str, n_done: int) -> None:
        checkpoints.append(
            _sample(
                label,
                calls_completed=n_done,
                call_runtime=call_runtime,
                media=media,
                db=db,
                fs_container=args.fs_container,
                wall_start=wall_start,
            )
        )

    try:
        sample("before_first_call", 0)
        for i, (fixture, scenario) in enumerate(zip(fixtures, scenarios, strict=True), start=1):
            print(f"\n[soak] === call {i}/{len(scenarios)} -- scenario={scenario} ===")
            port_slot = (i % 8) + 1
            code, evidence = await _run_call_on_shared_infra(
                fixture,
                db=db,
                sip_host=args.sip_host,
                sip_port=args.sip_port,
                sip_advertise_ip=args.sip_advertise_ip,
                local_sip_port=args.local_sip_port + port_slot,
                local_rtp_port=args.local_rtp_port + port_slot,
                early_hangup_seconds=(args.cancel_hangup_seconds if scenario == "cancel" else None),
                telephony=telephony,
                induce_media_stop=(scenario == "media_fail"),
            )
            outcomes.append(
                CallOutcome(
                    call_index=i,
                    scenario=scenario,
                    tenant_id=evidence.get("tenant_id", ""),
                    call_session_id=evidence.get("call_session_id"),
                    fs_channel_uuid=evidence.get("fs_channel_uuid"),
                    sip_call_id=evidence.get("sip_call_id"),
                    final_status=evidence.get("final_status"),
                    final_end_reason=evidence.get("final_end_reason"),
                    own_reply_present=evidence.get("own_reply_present"),
                    cross_contamination_detected=evidence.get("cross_contamination_detected"),
                    error_dict_size_after=len(call_runtime._errors),  # noqa: SLF001
                    runtime_load_after=call_runtime.current_load,
                    result_code=code,
                    raw_evidence=evidence,
                )
            )
            print(
                f"[soak] call {i} scenario={scenario} result_code={code} "
                f"final_status={evidence.get('final_status')!r} "
                f"end_reason={evidence.get('final_end_reason')!r} "
                f"runtime_errors_dict_size_now={len(call_runtime._errors)} "  # noqa: SLF001
                f"runtime_load_now={call_runtime.current_load}"
            )
            if i in {5, 10, 20} or i == len(scenarios):
                sample(f"after_{i}_calls", i)
    finally:
        router_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await router_task
        await esl.close()
        db.close()
        media_server.close()
        await media_server.wait_closed()

    print("\n[summary] per-call outcomes:")
    tenants = {o.tenant_id for o in outcomes}
    sessions = {o.call_session_id for o in outcomes}
    fs_uuids = {o.fs_channel_uuid for o in outcomes if o.fs_channel_uuid}
    sip_ids = {o.sip_call_id for o in outcomes if o.sip_call_id}
    for o in outcomes:
        print(
            f"  #{o.call_index:02d} scenario={o.scenario:10s} status={o.final_status!r} "
            f"end_reason={o.final_end_reason!r} session={o.call_session_id} "
            f"fs_uuid={o.fs_channel_uuid} sip_call_id={o.sip_call_id} "
            f"errors_dict_size_after={o.error_dict_size_after} "
            f"runtime_load_after={o.runtime_load_after} code={o.result_code}"
        )

    print(
        f"\n[isolation] distinct tenants={len(tenants)} distinct sessions={len(sessions)} "
        f"distinct fs_uuids={len(fs_uuids)} distinct sip_call_ids={len(sip_ids)} "
        f"(of {len(outcomes)} calls)"
    )
    identity_ok = len({len(outcomes), len(tenants), len(sessions)}) == 1
    print(f"[isolation] identity-reuse check: {'PASS' if identity_ok else 'FAIL'}")

    non_media_fail_normal = [o for o in outcomes if o.scenario == "normal"]
    normal_ok = all(o.final_status == "completed" for o in non_media_fail_normal)
    print(
        f"[normal-calls] {len(non_media_fail_normal)} normal-scenario calls, "
        f"all reached status=completed: {normal_ok}"
    )

    media_fail_calls = [o for o in outcomes if o.scenario == "media_fail"]
    media_fail_ok = all(o.final_status == "failed" for o in media_fail_calls)
    print(
        f"[media-fail-calls] {len(media_fail_calls)} media_fail-scenario calls, "
        f"all reached status=failed: {media_fail_ok}"
    )

    cancel_calls = [o for o in outcomes if o.scenario == "cancel"]
    cancel_ok = all(o.final_status in {"completed", "failed"} for o in cancel_calls)
    print(
        f"[cancel-calls] {len(cancel_calls)} cancel-scenario (early-hangup) calls, "
        f"all reached a real terminal status: {cancel_ok}"
    )

    print("\n[resource-checkpoints] summary:")
    for cp in checkpoints:
        print(
            f"  {cp.label:20s} calls={cp.calls_completed:3d} "
            f"rss={cp.rss_bytes / (1024 * 1024):.1f}MiB "
            f"asyncio_tasks={cp.asyncio_task_count} "
            f"runtime_errors_dict_size={cp.call_runtime_error_dict_size} "
            f"media_streams={cp.media_active_streams} "
            f"db_executor_threads={cp.db_executor_thread_count} "
            f"wall={cp.wall_elapsed_seconds}s"
        )

    if args.json_out:
        payload = {
            "outcomes": [
                {k: v for k, v in vars(o).items() if k != "raw_evidence"} for o in outcomes
            ],
            "checkpoints": [vars(cp) for cp in checkpoints],
        }
        _Path(args.json_out).write_text(json.dumps(payload, indent=2, default=str))
        print(f"\n[output] wrote {args.json_out}")

    overall_ok = identity_ok and normal_ok and media_fail_ok and cancel_ok
    return 0 if overall_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fs-host", default="127.0.0.1")
    parser.add_argument("--fs-esl-port", type=int, default=18023)
    parser.add_argument("--fs-password", default="ClueCon")
    parser.add_argument("--fs-container", default="", help="empty disables docker-stats sampling")
    parser.add_argument("--sip-host", default="127.0.0.1")
    parser.add_argument("--sip-port", type=int, default=15080)
    parser.add_argument("--media-public-base-url", required=True)
    parser.add_argument("--media-listen-host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--media-listen-port", type=int, default=8601)
    parser.add_argument("--sip-advertise-ip", required=True)
    parser.add_argument("--local-sip-port", type=int, default=15900)
    parser.add_argument("--local-rtp-port", type=int, default=16000)
    parser.add_argument(
        "--sequence",
        required=True,
        help=(
            "comma-separated scenario list, one per sequential call, e.g. "
            "'normal,normal,normal,normal,normal,media_fail,normal,cancel,normal,...' "
            "-- 'normal' (no induced failure), 'media_fail' (real per-call "
            "'api uuid_audio_stream <uuid> stop'), 'cancel' (real early caller BYE "
            "--cancel-hangup-seconds after the caller's utterance ends, instead of "
            "waiting the full AI round trip)."
        ),
    )
    parser.add_argument("--cancel-hangup-seconds", type=float, default=2.0)
    parser.add_argument(
        "--json-out", default="", help="path to write full JSON evidence; empty disables"
    )
    args = parser.parse_args()
    return asyncio.run(_run_soak(args))


if __name__ == "__main__":
    raise SystemExit(main())
