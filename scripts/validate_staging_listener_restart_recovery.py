#!/usr/bin/env python
"""Phase 2.35: real listener restart / recovery validation.

Builds on Phase 2.33's own `media_transport.py` fix (`listener_closing`
threaded through `serve_freeswitch_media()`/`handle_connection()`, see
`tests/telephony/freeswitch/test_media_transport.py`), which was only ever
validated hermetically. This script validates the real-staging operational
consequence against p224-freeswitch's own SHARED media listener (the one
`scripts/validate_staging_long_running_soak.py`-style calls depend on, not
an isolated one like `scripts/validate_staging_listener_disconnect.py`
deliberately builds for its own narrower purpose against p223):

    listener running -> real call in_progress -> listener-wide Server.close()
    -> active call fails failed/media_disconnect -> listener restarted and
    verified ready -> completely fresh real call -> completed/completed

Three tests, run sequentially against ONE shared ESL connection /
CallRuntime / DatabaseBoundary / CallOrchestrator (only the media listener
itself is torn down and rebuilt between Test 2 and Test 3) -- reuses
`_provision_fixture`/`_run_call_on_shared_infra`/`CallFixture` from
`validate_staging_concurrent_media_e2e.py` and `_sample`/`ResourceCheckpoint`
from `validate_staging_long_running_soak.py` unchanged, exactly as
`validate_staging_listener_disconnect.py` already does for its own narrower
scenario.

Staging-only test tooling, not product code, not imported by anything under
`voiceagent/`. Never prints a secret.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import socket
import sys as _sys
import time
from pathlib import Path as _Path

from core.identity import create_user

from voiceagent.calls.service import list_call_sessions
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
    _provision_fixture,
    _run_call_on_shared_infra,
)
from validate_staging_long_running_soak import ResourceCheckpoint, _sample  # noqa: E402

_logger = logging.getLogger(__name__)


async def _wait_until(predicate, *, timeout_seconds: float, interval_seconds: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval_seconds)
    return predicate()


def _probe_tcp_accept(host: str, port: int, *, timeout_seconds: float = 1.0) -> bool:
    """Real readiness evidence for the restarted listener: an actual TCP
    connect-and-close against `(host, port)`, never a bare `sleep()` or a
    process-exists check (brief: "Verify an actual readiness condition")."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host  # noqa: S104
    try:
        with socket.create_connection((probe_host, port), timeout=timeout_seconds) as s:
            s.close()
        return True
    except OSError:
        return False


async def _main(args: argparse.Namespace) -> int:
    print(
        f"[setup] shared topology against p224-freeswitch {args.fs_host}:{args.sip_port} "
        f"(ESL {args.fs_esl_port}), media listener on {args.media_listen_host}:"
        f"{args.media_listen_port}"
    )
    user = create_user()
    fixture1 = _provision_fixture(1, user.id, phrase_mode="distinct")
    fixture2 = _provision_fixture(2, user.id, phrase_mode="distinct")
    fixture3 = _provision_fixture(3, user.id, phrase_mode="distinct")
    print(
        f"[setup] fixture1 tenant={fixture1.tenant_id} phone={fixture1.e164}\n"
        f"[setup] fixture2 tenant={fixture2.tenant_id} phone={fixture2.e164}\n"
        f"[setup] fixture3 tenant={fixture3.tenant_id} phone={fixture3.e164}"
    )

    esl = ManagedEslConnection(
        host=args.fs_host, port=args.fs_esl_port, password_provider=lambda: args.fs_password
    )
    await esl.start()
    media_ticket_secret = "phase235-listener-restart-secret"  # noqa: S105  # pragma: allowlist secret
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
    db = DatabaseBoundary(max_workers=4)
    heartbeats = FakeHeartbeatStore()
    instance_id = "phase235-listener-restart-runtime"
    await heartbeats.write(
        RuntimeHeartbeat(
            instance_id=instance_id,
            address=f"{instance_id}:0",
            capacity=5,
            current_load=0,
            last_heartbeat_epoch_seconds=0.0,
        ),
        ttl_seconds=600.0,
    )
    call_runtime = CallRuntime(
        instance_id=instance_id, address=f"{instance_id}:0", capacity=5, heartbeat_store=heartbeats
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
    checkpoints: list[ResourceCheckpoint] = []

    def sample(label: str) -> None:
        checkpoints.append(
            _sample(
                label,
                calls_completed=0,
                call_runtime=call_runtime,
                media=media,
                db=db,
                fs_container=args.fs_container,
                wall_start=wall_start,
            )
        )

    result: dict = {"checkpoints": {}, "tests": {}}
    exit_code = 0

    try:
        # ------------------------------------------------------------ A --
        sample("A_before_test1")

        # =========================================== Test 1: baseline ====
        print("\n[test-1] baseline call against the live shared listener ...")
        t1_task = asyncio.create_task(
            _run_call_on_shared_infra(
                fixture1,
                db=db,
                sip_host=args.sip_host,
                sip_port=args.sip_port,
                sip_advertise_ip=args.sip_advertise_ip,
                local_sip_port=args.local_sip_port + 1,
                local_rtp_port=args.local_rtp_port + 1,
                telephony=telephony,
            )
        )
        await asyncio.sleep(4.0)
        sample("B_during_test1")  # call 1 is mid-flight (SIP up, media attaching/attached)
        try:
            code1, evidence1 = await asyncio.wait_for(t1_task, timeout=40.0)
        except TimeoutError:
            print(
                "[test-1] FAIL: call 1 never reached a terminal state within 40s -- "
                "it appears to have hung rather than failing cleanly"
            )
            t1_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t1_task
            code1, evidence1 = None, {}
        sample("C_after_test1")
        result["tests"]["test1_baseline"] = {
            "result_code": code1,
            "final_status": evidence1.get("final_status"),
            "final_end_reason": evidence1.get("final_end_reason"),
            "call_session_id": evidence1.get("call_session_id"),
            "fs_channel_uuid": evidence1.get("fs_channel_uuid"),
            "sip_call_id": evidence1.get("sip_call_id"),
            "tenant_id": evidence1.get("tenant_id"),
            "own_reply_present": evidence1.get("own_reply_present"),
            "real_assistant_reply": evidence1.get("real_assistant_reply"),
        }
        t1_pass = evidence1.get("final_status") == "completed" and evidence1.get(
            "final_end_reason"
        ) in ("completed", None)
        print(f"[test-1] {'PASS' if t1_pass else 'FAIL'}: {result['tests']['test1_baseline']}")
        if not t1_pass:
            exit_code = 1

        # ================================ Test 2: listener-wide close ====
        print("\n[test-2] second call, then closing the SHARED media listener mid-call ...")
        t2_task = asyncio.create_task(
            _run_call_on_shared_infra(
                fixture2,
                db=db,
                sip_host=args.sip_host,
                sip_port=args.sip_port,
                sip_advertise_ip=args.sip_advertise_ip,
                local_sip_port=args.local_sip_port + 2,
                local_rtp_port=args.local_rtp_port + 2,
                telephony=telephony,
            )
        )

        def _call2_in_progress() -> bool:
            rows = list_call_sessions(fixture2.context, limit=1)
            return bool(rows) and rows[0].status == "in_progress"

        evidence2: dict = {}
        reached_in_progress = await _wait_until(_call2_in_progress, timeout_seconds=15.0)
        if not reached_in_progress:
            print(
                "[test-2] FAIL: call 2 never reached a real in_progress/media-attached state "
                "within 15s -- aborting before touching the listener"
            )
            t2_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t2_task
            result["tests"]["test2_listener_close"] = {"error": "call2_never_in_progress"}
            exit_code = 1
        else:
            print(
                "[test-2] call 2 confirmed in_progress/media-attached -- "
                "letting audio flow a beat ..."
            )
            await asyncio.sleep(1.5)
            sample("D_during_test2_before_close")

            print(
                "[test-2] closing the SHARED media_server.close() NOW (same mechanism as "
                "tests/telephony/freeswitch/test_media_transport.py's own "
                "listener_closing path) ..."
            )
            closed_at = time.monotonic()
            media_server.close()
            await media_server.wait_closed()
            close_elapsed = time.monotonic() - closed_at
            print(
                "[test-2] listener server.close()/wait_closed() returned "
                f"after {close_elapsed:.2f}s"
            )
            sample("E_immediately_after_listener_close")

            try:
                code2, evidence2 = await asyncio.wait_for(t2_task, timeout=40.0)
                terminal_elapsed = time.monotonic() - closed_at
                sample("F_after_test2_terminal")
                result["tests"]["test2_listener_close"] = {
                    "result_code": code2,
                    "final_status": evidence2.get("final_status"),
                    "final_end_reason": evidence2.get("final_end_reason"),
                    "call_session_id": evidence2.get("call_session_id"),
                    "fs_channel_uuid": evidence2.get("fs_channel_uuid"),
                    "sip_call_id": evidence2.get("sip_call_id"),
                    "tenant_id": evidence2.get("tenant_id"),
                    "seconds_from_close_to_terminal": round(terminal_elapsed, 2),
                }
                t2_pass = (
                    evidence2.get("final_status") == "failed"
                    and evidence2.get("final_end_reason") == "media_disconnect"
                )
                print(
                    f"[test-2] {'PASS' if t2_pass else 'FAIL'}: "
                    f"{result['tests']['test2_listener_close']}"
                )
                if not t2_pass:
                    exit_code = 1
            except TimeoutError:
                print(
                    "[test-2] FAIL: call 2 never reached a terminal state within 40s of the "
                    "listener closing -- it appears to have hung rather than failing cleanly"
                )
                t2_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t2_task
                sample("F_after_test2_terminal")
                result["tests"]["test2_listener_close"] = {"error": "call2_never_reached_terminal"}
                exit_code = 1

        # ===================================== Restart the listener  =====
        print("\n[restart] rebuilding the media listener on the same port ...")
        restart_started_at = time.monotonic()
        new_media_listener = FreeSwitchMediaListener(media, ticket_secret=media_ticket_secret)
        new_media_server = None
        last_bind_error: Exception | None = None
        for attempt in range(1, 11):
            try:
                new_media_server = await serve_freeswitch_media(
                    new_media_listener, host=args.media_listen_host, port=args.media_listen_port
                )
                break
            except OSError as exc:  # noqa: PERF203 -- short, bounded bind-retry loop
                last_bind_error = exc
                print(f"[restart] bind attempt {attempt} failed ({exc}); retrying ...")
                await asyncio.sleep(1.0)
        if new_media_server is None:
            print(f"[restart] FAIL: could not rebind the listener port: {last_bind_error}")
            result["restart"] = {"error": f"bind_failed: {last_bind_error}"}
            exit_code = 1
        else:
            media_server = new_media_server
            ready = await _wait_until(
                lambda: _probe_tcp_accept(args.media_listen_host, args.media_listen_port),
                timeout_seconds=10.0,
            )
            restart_elapsed = time.monotonic() - restart_started_at
            print(
                f"[restart] rebind took {restart_elapsed:.2f}s; real TCP-connect readiness probe "
                f"against {args.media_listen_host}:{args.media_listen_port} = {ready}"
            )
            result["restart"] = {
                "rebind_seconds": round(restart_elapsed, 2),
                "tcp_accept_probe_passed": ready,
            }
            sample("G_after_listener_restart")
            if not ready:
                exit_code = 1

            # ============================ Test 3: fresh call recovers ====
            if ready:
                print("\n[test-3] completely fresh call through the restarted listener ...")
                try:
                    code3, evidence3 = await asyncio.wait_for(
                        _run_call_on_shared_infra(
                            fixture3,
                            db=db,
                            sip_host=args.sip_host,
                            sip_port=args.sip_port,
                            sip_advertise_ip=args.sip_advertise_ip,
                            local_sip_port=args.local_sip_port + 3,
                            local_rtp_port=args.local_rtp_port + 3,
                            telephony=telephony,
                        ),
                        timeout=40.0,
                    )
                except TimeoutError:
                    print(
                        "[test-3] FAIL: call 3 never reached a terminal state within 40s -- "
                        "it appears to have hung rather than failing cleanly"
                    )
                    code3, evidence3 = None, {}
                sample("H_after_test3_terminal")
                result["tests"]["test3_recovery"] = {
                    "result_code": code3,
                    "final_status": evidence3.get("final_status"),
                    "final_end_reason": evidence3.get("final_end_reason"),
                    "call_session_id": evidence3.get("call_session_id"),
                    "fs_channel_uuid": evidence3.get("fs_channel_uuid"),
                    "sip_call_id": evidence3.get("sip_call_id"),
                    "tenant_id": evidence3.get("tenant_id"),
                    "own_reply_present": evidence3.get("own_reply_present"),
                    "real_assistant_reply": evidence3.get("real_assistant_reply"),
                }
                t3_pass = evidence3.get("final_status") == "completed" and evidence3.get(
                    "final_end_reason"
                ) in ("completed", None)
                print(
                    f"[test-3] {'PASS' if t3_pass else 'FAIL'}: {result['tests']['test3_recovery']}"
                )
                if not t3_pass:
                    exit_code = 1

                ids = {
                    "fixture1_tenant": str(fixture1.tenant_id),
                    "fixture2_tenant": str(fixture2.tenant_id),
                    "fixture3_tenant": str(fixture3.tenant_id),
                }
                no_reuse = len(set(ids.values())) == 3
                result["identifier_isolation"] = {
                    "tenants": ids,
                    "fs_uuids": [
                        evidence1.get("fs_channel_uuid"),
                        evidence2.get("fs_channel_uuid") if reached_in_progress else None,
                        evidence3.get("fs_channel_uuid"),
                    ],
                    "sip_call_ids": [
                        evidence1.get("sip_call_id"),
                        evidence2.get("sip_call_id") if reached_in_progress else None,
                        evidence3.get("sip_call_id"),
                    ],
                    "tenants_distinct": no_reuse,
                }
                print(f"[identifier-isolation] {result['identifier_isolation']}")
                if not no_reuse:
                    exit_code = 1

        result["checkpoints"] = {
            cp.label: {
                "asyncio_task_count": cp.asyncio_task_count,
                "call_runtime_current_load": cp.call_runtime_current_load,
                "call_runtime_error_dict_size": cp.call_runtime_error_dict_size,
                "call_runtime_cancellation_dict_size": cp.call_runtime_cancellation_dict_size,
                "media_active_streams": cp.media_active_streams,
                "media_active_sockets": cp.media_active_sockets,
                "db_executor_thread_count": cp.db_executor_thread_count,
                "rss_mib": round(cp.rss_bytes / (1024 * 1024), 1),
            }
            for cp in checkpoints
        }

    finally:
        router_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await router_task
        await esl.close()
        db.close()
        with contextlib.suppress(Exception):
            media_server.close()
            await media_server.wait_closed()

    print("\n[result-json]")
    print(json.dumps(result, indent=2, default=str))
    print(f"\n[verdict] exit_code={exit_code}")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fs-host", default="127.0.0.1")
    parser.add_argument("--fs-esl-port", type=int, default=18023)
    parser.add_argument("--fs-password", default="ClueCon")
    parser.add_argument("--fs-container", default="p224-freeswitch")
    parser.add_argument("--sip-host", default="127.0.0.1")
    parser.add_argument("--sip-port", type=int, default=15080)
    parser.add_argument("--media-public-base-url", required=True)
    parser.add_argument("--media-listen-host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--media-listen-port", type=int, default=8601)
    parser.add_argument("--sip-advertise-ip", required=True)
    parser.add_argument("--local-sip-port", type=int, default=15950)
    parser.add_argument("--local-rtp-port", type=int, default=16050)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
