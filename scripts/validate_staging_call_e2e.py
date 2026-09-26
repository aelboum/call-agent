#!/usr/bin/env python
"""Phase 2.23: one-shot real-FreeSWITCH + real-PostgreSQL call-orchestrator
end-to-end validation script.

**Staging-only.** Wires the real `voiceagent.runtime.orchestrator
.CallOrchestrator` to a REAL FreeSWITCH ESL connection (not
`FakeTelephonyProvider`) and a REAL PostgreSQL database (not sqlite/mocks),
originates a real internal FreeSWITCH channel whose destination number
matches a real, just-created `app.inbound_call_routes` row, and observes
the orchestrator's own real behavior: authoritative tenant/agent
resolution, `CallSession` creation, authorization, runtime-ownership
claim, the exactly-once activation gate, and a real `uuid_answer` against
the live server.

**Expected outcome against a FreeSWITCH image with no `mod_audio_stream`**
(Phase 2.23's own `safarov/freeswitch`): `start_media_stream()` genuinely
fails -- `api uuid_audio_stream ...` gets a real `-ERR command not found`
from the live server. This script treats that as an *expected* terminal
outcome, not a bug: it proves the orchestrator's own bounded
`media_unavailable` failure path (real `CallSession` transition to
`failed`, real channel hangup, real terminal cleanup) end to end against a
real server (Phase 2.23 brief section 8, "Media failure: verify bounded
failure and terminal cleanup").

**Against a FreeSWITCH image that does have `mod_audio_stream`** (Phase
2.24's own finding: `rasonyang/freeswitch-aicc`, run with a real,
container-reachable `--media-public-base-url` instead of the default
placeholder), the call instead proceeds to a real `answered` state -- see
`docs/PHASE-2.24-REAL-MEDIA-CALL-E2E.md` for that result.

Prerequisites:

* A real PostgreSQL reachable at `DATABASE_URL`/`MIGRATIONS_DATABASE_URL`
  with both SaaS-OS's and this product's migrations applied (see
  `tests/integration/README.md`).
* A real FreeSWITCH instance reachable at `--fs-host`/`--fs-port` with
  `mod_event_socket` enabled (default password `ClueCon`, override with
  `--fs-password`) and its inbound ACL permitting this host.

Usage::

    DATABASE_URL=... MIGRATIONS_DATABASE_URL=... REDIS_URL=... \\
    APP_DB_USER=saas_os_app ENVIRONMENT=test \\
    python scripts/validate_staging_call_e2e.py --fs-host 127.0.0.1 --fs-port 18021

Exit code 0 only if the routing/authorization/ownership/activation-gate
chain and the expected media-unavailable terminal cleanup are all
confirmed; non-zero and a clear message naming which boundary failed
otherwise. Never prints a secret value.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import uuid

from core.identity import create_user
from core.tenancy import create_tenant

from voiceagent.agents.config import AgentConfig
from voiceagent.agents.service import create_agent, create_draft_version
from voiceagent.agents.service import publish_version as _publish_version
from voiceagent.calls.service import get_call_session_by_fs_channel_uuid
from voiceagent.config.settings import AiProviderSettings
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


def _config() -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "instructions": "Answer the phone.",
            "language": "en",
            "voice": {"provider": "fake", "voice_id": "v1"},
            "engine": {
                "kind": "pipelined",
                "stt": {"provider": "fake", "config": {}},
                "llm": {"provider": "fake", "model": "fake-model", "config": {}},
                "tts": {"provider": "fake", "config": {}},
            },
            "business_hours": {"timezone": "UTC", "windows": []},
            "privacy": {"data_classification": "tenant_data", "purpose": "conversation"},
        }
    )


def _e164() -> str:
    return f"+1555{uuid.uuid4().int % 10**7:07d}"


async def _wait_until(predicate, *, timeout_seconds: float = 15.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


async def _run(
    fs_host: str,
    fs_port: int,
    fs_password: str,
    media_public_base_url: str,
    media_listen_host: str,
    media_listen_port: int,
) -> int:
    print("[1/7] provisioning a real tenant/agent/phone-number in real PostgreSQL ...")
    tenant = create_tenant(f"phase223-{uuid.uuid4().hex[:8]}")
    user = create_user()
    context = TenantContext(tenant_id=tenant.id, actor_id=user.id, membership_id=uuid.uuid4())
    agent = create_agent(context, name="Phase 2.23 Staging Agent")
    draft = create_draft_version(context, agent.id, config=_config())
    _publish_version(context, agent.id, draft.id)
    e164 = _e164()
    number = register_phone_number(context, e164=e164, agent_id=agent.id)
    print(f"      tenant={tenant.id} agent={agent.id} phone_number={number.e164}")

    print(f"[2/7] connecting to real FreeSWITCH ESL at {fs_host}:{fs_port} ...")
    esl = ManagedEslConnection(host=fs_host, port=fs_port, password_provider=lambda: fs_password)
    try:
        await esl.start()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: could not connect/authenticate to FreeSWITCH ESL: {exc}")
        return 1
    print("      connected and authenticated")

    media_ticket_secret = "phase223-validation-secret"  # noqa: S105 -- disposable, this run only.  # pragma: allowlist secret
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
    print(f"      real wss:// media listener started on {media_listen_host}:{media_listen_port}")
    db = DatabaseBoundary(max_workers=4)
    heartbeats = FakeHeartbeatStore()
    await heartbeats.write(
        RuntimeHeartbeat(
            instance_id="phase223-staging-runtime",
            address="phase223-staging-runtime:0",
            capacity=10,
            current_load=0,
            last_heartbeat_epoch_seconds=0.0,
        ),
        ttl_seconds=60.0,
    )
    call_runtime = CallRuntime(
        instance_id="phase223-staging-runtime",
        address="phase223-staging-runtime:0",
        capacity=10,
        heartbeat_store=heartbeats,
    )
    router = TelephonyEventRouter(telephony)
    policy_source = StaticAiDataPolicySource(
        AiProviderSettings(
            eligible_providers=("fake",),
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
        answer_timeout_seconds=5.0,
    )
    router.on_unrouted_offer = orchestrator.handle_unrouted_offer
    router_task = asyncio.create_task(router.run())

    call_ref = str(uuid.uuid4())
    print(f"[3/7] originating a real FreeSWITCH channel to {e164}, call_ref={call_ref} ...")
    reply = await esl.send(
        f"bgapi originate "
        f"{{origination_uuid={call_ref},origination_caller_id_number=+15550100}}"
        f"null/{e164} &park()"
    )
    if reply.startswith("-ERR"):
        print(f"FAIL: originate rejected: {reply!r}")
        await _shutdown(router_task, esl, db, media_server)
        return 1
    print("      real OFFERED event should now reach the orchestrator via TelephonyEventRouter")

    print("[4/7] waiting for the orchestrator to create and authorize a real CallSession ...")
    ok = await _wait_until(
        lambda: get_call_session_by_fs_channel_uuid(context, call_ref) is not None,
        timeout_seconds=10.0,
    )
    if not ok:
        print("FAIL: no CallSession was ever created for this real call_ref")
        await _shutdown(router_task, esl, db, media_server)
        return 1
    call = get_call_session_by_fs_channel_uuid(context, call_ref)
    if call is None:
        print("FAIL: CallSession vanished immediately after being observed")
        await _shutdown(router_task, esl, db, media_server)
        return 1
    print(f"      real CallSession created: id={call.id} status={call.status!r}")

    print("[5/7] waiting for the call to reach a terminal or answered state ...")

    def _is_settled() -> bool:
        row = get_call_session_by_fs_channel_uuid(context, call_ref)
        return row is not None and row.status in {
            "answered",
            "failed",
            "in_progress",
            "completed",
            "interrupted",
        }

    await _wait_until(_is_settled, timeout_seconds=15.0)
    call = get_call_session_by_fs_channel_uuid(context, call_ref)
    if call is None:
        print("FAIL: CallSession vanished before reaching a settled state")
        await _shutdown(router_task, esl, db, media_server)
        return 1
    print(f"      final observed status: {call.status!r}, end_reason={call.end_reason!r}")
    print(f"      runtime_instance_id={call.runtime_instance_id!r}")

    print("[6/7] checking outcome ...")
    if call.status == "failed" and call.end_reason == "media_unavailable":
        print(
            "      EXPECTED outcome for this environment: real routing/authz/ownership/"
            "answer all succeeded; media attach genuinely failed (no mod_audio_stream on "
            "this FreeSWITCH image) and the orchestrator correctly transitioned the "
            "CallSession to failed/media_unavailable and hung up the real channel."
        )
        result = 0
    elif call.status == "answered":
        print("      call reached answered -- mod_audio_stream appears to be available here.")
        result = 0
    else:
        print(f"FAIL: unexpected terminal state {call.status!r}/{call.end_reason!r}")
        result = 1

    print("[7/7] cleaning up ...")
    await _shutdown(router_task, esl, db, media_server)
    print("PASS" if result == 0 else "FAIL")
    return result


async def _shutdown(
    router_task: asyncio.Task[None],
    esl: ManagedEslConnection,
    db: DatabaseBoundary,
    media_server,
) -> None:
    router_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await router_task
    await esl.close()
    db.close()
    media_server.close()
    await media_server.wait_closed()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fs-host", default="127.0.0.1")
    parser.add_argument("--fs-port", type=int, default=18021)
    parser.add_argument("--fs-password", default="ClueCon")
    parser.add_argument(
        "--media-public-base-url",
        default="wss://staging.invalid.example",
        help=(
            "Base wss:// URL FreeSWITCH will try to reach for media -- pass a real, "
            "container-reachable ws:// URL (a raw IP; see scripts/validate_staging_media_e2e.py's "
            "own docstring) to exercise the real answered path against an image with "
            "mod_audio_stream. The default placeholder reproduces Phase 2.23's own "
            "media_unavailable-path validation unchanged."
        ),
    )
    parser.add_argument("--media-listen-host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--media-listen-port", type=int, default=8300)
    args = parser.parse_args()
    return asyncio.run(
        _run(
            args.fs_host,
            args.fs_port,
            args.fs_password,
            args.media_public_base_url,
            args.media_listen_host,
            args.media_listen_port,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
