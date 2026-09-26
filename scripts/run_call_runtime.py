#!/usr/bin/env python
"""Production entrypoint for one `voiceagent.runtime.supervisor.CallRuntime`
process (ADR-0008 point 1: one `call-runtime` process, one event loop, many
concurrent calls; horizontal scaling is more processes, never more calls per
process beyond `capacity`).

**What this closes**: `docs/PHASE-2.14-STATUS.md` section 12 documented that
"no entrypoint script exists yet for either process" (the call-runtime or a
worker) -- this script is that entrypoint for the call-runtime. It wires a
real `CallRuntime` to a real Redis-backed heartbeat store
(`RedisHeartbeatStore`, the same class `voiceagent/api/v1/ops.py` already
reads from) and gives it bounded, signal-driven startup and shutdown.

**Phase 2.22 closes the remaining gap**: whenever FreeSWITCH is configured,
this script now also constructs a `voiceagent.runtime.orchestrator
.CallOrchestrator` around the real transport and wires it as the real
FreeSWITCH connection's own unrouted-`OFFERED` handler
(`TelephonyEventRouter.on_unrouted_offer`) -- an inbound call is now
authoritatively routed, authorized, and handed to `CallRuntime.start_call()`
by this same process, not merely heartbeating and connected. Outbound-call
origination requests remain out of this phase's scope (see
`docs/PHASE-2.22-CALL-ORCHESTRATION.md`'s "Known limitations").

Usage::

    python scripts/run_call_runtime.py

Configuration (environment): every ordinary `voiceagent` process variable
(`DATABASE_URL`, `REDIS_URL`, `ENVIRONMENT`, ...) plus
`VOICEAGENT_RUNTIME_ADDRESS` (this instance's own host:port, advertised in
its heartbeat for `voiceagent.runtime.assignment`'s least-loaded selection;
defaults to the local hostname). `VOICEAGENT_RUNTIME_MAX_CONCURRENT_CALLS`,
`VOICEAGENT_RUNTIME_HEARTBEAT_INTERVAL_SECONDS` and
`VOICEAGENT_RUNTIME_HEARTBEAT_TTL_SECONDS` (already read by
`voiceagent.config.settings_from_env()`) tune capacity and heartbeat timing.
`VOICEAGENT_FREESWITCH_ESL_HOST` (plus `_ESL_PORT`, `_MEDIA_LISTEN_HOST`,
`_MEDIA_LISTEN_PORT`) and the `FREESWITCH_ESL_PASSWORD`/
`FREESWITCH_MEDIA_TICKET_SECRET` secrets (`infra.secrets`, never `Settings`)
enable the real telephony transport; leaving `_ESL_HOST` unset keeps this
process exactly as it was before Phase 2.21 (heartbeat only).

Shutdown: SIGTERM or SIGINT calls `CallRuntime.shutdown()` (cancels every
owned call concurrently, bounded per-call), closes the FreeSWITCH transport
if it was started (`ManagedEslConnection.close()`, the media listener), and
deregisters this instance's heartbeat before the process exits -- a clean
stop is never mistaken for a crash by the reconciler.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket

from infra.jobs.config import get_jobs_config
from infra.secrets import get_secrets_provider

from voiceagent.config import Settings, settings_from_env, validate_deployment_readiness
from voiceagent.runtime.conversation_persistence import ConversationPersistence
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.runtime.heartbeat import RedisHeartbeatStore, new_instance_id
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

_logger = logging.getLogger("voiceagent.scripts.run_call_runtime")


class _FreeSwitchTransport:
    """Everything Phase 2.21's real FreeSWITCH transport needs started and
    stopped together -- constructed only when `settings.freeswitch
    .is_configured`. `_run()` builds the Phase 2.22 `CallOrchestrator`
    around this transport's own `telephony`/`media`/`events` right after
    constructing it (and before `start()`), wiring
    `self.events.on_unrouted_offer = orchestrator.handle_unrouted_offer`."""

    def __init__(self, settings: Settings) -> None:
        self.esl = ManagedEslConnection(
            host=settings.freeswitch.esl_host,  # type: ignore[arg-type] -- only constructed when set.
            port=settings.freeswitch.esl_port,
            password_provider=lambda: get_secrets_provider().get_required(
                "FREESWITCH_ESL_PASSWORD"
            ),
        )
        self.telephony = FreeSwitchTelephonyProvider(
            self.esl,
            command_timeout_seconds=settings.freeswitch.command_timeout_seconds,
            media_public_base_url=settings.freeswitch.media_public_url,
            media_ticket_secret_provider=lambda: get_secrets_provider().get_required(
                "FREESWITCH_MEDIA_TICKET_SECRET"
            ),
        )
        self.media = FreeSwitchMediaProvider()
        self.media_listener = FreeSwitchMediaListener(
            self.media,
            ticket_secret=get_secrets_provider().get_required("FREESWITCH_MEDIA_TICKET_SECRET"),
        )
        self.events = TelephonyEventRouter(self.telephony)
        self._listen_host = settings.freeswitch.media_listen_host
        self._listen_port = settings.freeswitch.media_listen_port
        self._media_server = None
        self._events_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        # Bounded, single attempt (brief section 5: "bounded connection
        # establishment") -- a failure here is a real deployment problem
        # (FreeSWITCH unreachable, wrong credentials) and this process
        # fails closed rather than starting half-connected; `ManagedEslConnection`
        # itself reconnects with backoff for every disconnect *after* this
        # first success.
        await self.esl.start()
        self._media_server = await serve_freeswitch_media(
            self.media_listener, host=self._listen_host, port=self._listen_port
        )
        self._events_task = asyncio.create_task(self.events.run())
        _logger.info(
            "call_runtime.freeswitch_transport.started",
            extra={"media_listen_host": self._listen_host, "media_listen_port": self._listen_port},
        )

    async def stop(self) -> None:
        if self._events_task is not None:
            self._events_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._events_task
        if self._media_server is not None:
            self._media_server.close()
            await self._media_server.wait_closed()
        await self.esl.close()


async def _run() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = settings_from_env()
    validate_deployment_readiness(settings)
    address = _env("VOICEAGENT_RUNTIME_ADDRESS", socket.gethostname())

    heartbeat_store = RedisHeartbeatStore(get_jobs_config().redis_url)
    runtime = CallRuntime(
        instance_id=new_instance_id(),
        address=address,
        capacity=settings.runtime.max_concurrent_calls,
        heartbeat_store=heartbeat_store,
    )
    runtime.start_heartbeat(
        interval_seconds=settings.runtime.heartbeat_interval_seconds,
        ttl_seconds=settings.runtime.heartbeat_ttl_seconds,
    )

    db = DatabaseBoundary(max_workers=settings.runtime.to_thread_pool_size)
    orchestrator: CallOrchestrator | None = None
    freeswitch_transport = (
        _FreeSwitchTransport(settings) if settings.freeswitch.is_configured else None
    )
    if freeswitch_transport is not None:
        orchestrator = CallOrchestrator(
            db=db,
            call_runtime=runtime,
            telephony=freeswitch_transport.telephony,
            media=freeswitch_transport.media,
            telephony_events=freeswitch_transport.events,
            heartbeat_store=heartbeat_store,
            policy_source=StaticAiDataPolicySource(settings.ai_providers),
            tool_gateway=ToolGateway(),
            conversation_persistence=ConversationPersistence(db),
            system_actor_user_id=settings.runtime.system_actor_user_id,
            system_service_account_name=settings.runtime.system_service_account_name,
        )
        freeswitch_transport.events.on_unrouted_offer = orchestrator.handle_unrouted_offer
        await freeswitch_transport.start()

    _logger.info(
        "call_runtime.started",
        extra={
            "instance_id": runtime.instance_id,
            "address": address,
            "capacity": runtime.capacity,
            "freeswitch_configured": freeswitch_transport is not None,
        },
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _request_stop)

    await stop_event.wait()
    _logger.info("call_runtime.shutdown.begin", extra={"instance_id": runtime.instance_id})
    if orchestrator is not None:
        await orchestrator.shutdown()
    await runtime.shutdown()
    if freeswitch_transport is not None:
        await freeswitch_transport.stop()
    await heartbeat_store.close()
    db.close()
    _logger.info("call_runtime.shutdown.complete", extra={"instance_id": runtime.instance_id})


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
