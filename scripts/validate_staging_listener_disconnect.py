#!/usr/bin/env python
"""Phase 2.31 section E: shared FreeSWITCH media listener disconnect,
validated safely and in isolation.

`voiceagent.telephony.freeswitch.media_transport.FreeSwitchMediaListener`
(via `serve_freeswitch_media()`) is ONE TCP/WebSocket server shared by
every call a given `CallRuntime` process handles (Phase 2.0's own design --
one listener port, ticket-routed per call, never one port per call). That
sharing is exactly why Phase 2.30 did not validate what happens when the
listener itself goes down: doing so against the same shared listener the
main long-running soak (`scripts/validate_staging_long_running_soak.py`)
depends on would have taken down every other call in that soak too, which
the brief for this phase explicitly forbids ("Do NOT destroy the shared
listener during a multi-call production test merely to satisfy coverage").

This script gets a real answer anyway, safely, by never touching that
shared listener at all: it builds its OWN, completely separate, one-call
topology -- its own `ManagedEslConnection`, its own `FreeSwitchMediaProvider`
/ `FreeSwitchMediaListener` (its own dedicated port), its own `CallRuntime`
/ `DatabaseBoundary` / `CallOrchestrator` -- against `p223-freeswitch`
(confirmed unused by every other Phase 2.27-2.31 real-staging script,
which all deliberately stay on `p224-freeswitch`), runs exactly one real
call through it, and once that call has reached real `in_progress` (media
attached, caller audio actively streaming), closes THIS SCRIPT'S OWN
listener's TCP server out from under that one call -- nothing else is
running against this listener, this `CallRuntime`, or this
`DatabaseBoundary`, so nothing else can be affected by closing it.

Expected behavior, given Phase 2.29's own fix (`voiceagent/runtime
/call_task.py`): the call's own `_FreeSwitchMediaStream.send()` (or
`.receive()`) should raise `TransportError` once its WebSocket is
severed by the listener going away, which `run_call_task()`'s own
`except TransportError` clause (Phase 2.29) classifies as
`cancellation.reason = "media_disconnect"`, finalizing the `CallSession`
as `status="failed"`, `end_reason="media_disconnect"` -- the same
classification as a single call's own media stop (Phase 2.29 section 2),
because from inside that one call's `MediaSocket`, "the listener is gone"
and "my own socket died" are the same observable event: the underlying
transport is closed either way.

Staging-only test tooling, not product code, not imported by anything
under `voiceagent/`. Never touches `p224-freeswitch` or any infra any
other script/process is using. Never prints a secret.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys as _sys
import time
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
    _provision_fixture,
    _run_call_on_shared_infra,
)


async def _main(args: argparse.Namespace) -> int:
    print(
        f"[setup] this script's OWN isolated topology against {args.fs_host}:{args.sip_port} "
        f"(ESL {args.fs_esl_port}) -- never touches p224-freeswitch or the main soak's listener"
    )
    user = create_user()
    fixture = _provision_fixture(1, user.id, phrase_mode="distinct")

    esl = ManagedEslConnection(
        host=args.fs_host, port=args.fs_esl_port, password_provider=lambda: args.fs_password
    )
    await esl.start()
    media_ticket_secret = "phase231-listener-disconnect-secret"  # noqa: S105  # pragma: allowlist secret
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
    instance_id = "phase231-listener-disconnect-runtime"
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

    call_task = asyncio.create_task(
        _run_call_on_shared_infra(
            fixture,
            db=db,
            sip_host=args.sip_host,
            sip_port=args.sip_port,
            sip_advertise_ip=args.sip_advertise_ip,
            local_sip_port=args.local_sip_port,
            local_rtp_port=args.local_rtp_port,
            telephony=telephony,
        )
    )

    try:
        print(
            f"[wait] letting the one real call reach in_progress/media-attached "
            f"before closing THIS SCRIPT'S OWN listener ({args.listener_kill_delay_seconds}s) ..."
        )
        await asyncio.sleep(args.listener_kill_delay_seconds)

        print("[action] closing this script's own dedicated media listener's TCP server NOW ...")
        killed_at = time.monotonic()
        media_server.close()
        await media_server.wait_closed()
        print(f"[action] listener closed at +{args.listener_kill_delay_seconds}s")

        code, evidence = await asyncio.wait_for(call_task, timeout=40.0)
        elapsed = time.monotonic() - killed_at
        print(f"\n[result] call reached a terminal outcome {elapsed:.1f}s after listener close")
        print(f"[result] code={code} evidence={evidence}")

        status = evidence.get("final_status")
        end_reason = evidence.get("final_end_reason")
        print(f"\n[verdict] final_status={status!r} end_reason={end_reason!r}")
        if status == "failed" and end_reason == "media_disconnect":
            print(
                "[verdict] PASS -- listener-level disconnect was classified identically to a "
                "per-call media disconnect (Phase 2.29's own TransportError -> media_disconnect "
                "path), reached a clean terminal CallSession status, did not hang, did not crash "
                "this process."
            )
            return 0
        print(
            f"[verdict] FAIL -- expected status='failed'/end_reason='media_disconnect', "
            f"got status={status!r}/end_reason={end_reason!r}"
        )
        return 1
    except TimeoutError:
        print(
            "\n[verdict] FAIL -- the call never reached a terminal state within 40s of the "
            "listener closing; it appears to have hung rather than failing cleanly."
        )
        call_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await call_task
        return 1
    finally:
        router_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await router_task
        await esl.close()
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fs-host", default="127.0.0.1", help="p223-freeswitch, never p224 (main soak's own)"
    )
    parser.add_argument("--fs-esl-port", type=int, default=18021)
    parser.add_argument("--fs-password", default="ClueCon")
    parser.add_argument("--sip-host", default="127.0.0.1")
    parser.add_argument("--sip-port", type=int, default=15060)
    parser.add_argument("--media-public-base-url", required=True)
    parser.add_argument("--media-listen-host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--media-listen-port", type=int, default=8701)
    parser.add_argument("--sip-advertise-ip", required=True)
    parser.add_argument("--local-sip-port", type=int, default=15990)
    parser.add_argument("--local-rtp-port", type=int, default=16090)
    parser.add_argument(
        "--listener-kill-delay-seconds",
        type=float,
        default=5.0,
        help="wait this long after starting the call before closing this script's own listener",
    )
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
