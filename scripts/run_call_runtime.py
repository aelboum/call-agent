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

**What this does NOT close, and does not attempt to**: this process has no
call to run until something calls `runtime.start_call()`. That call is
supposed to come from consuming a real FreeSWITCH ESL event stream
(`voiceagent.telephony.freeswitch.esl.EslConnection`), and that protocol has
only ever had a `Protocol` definition and a test fake
(`voiceagent.telephony.freeswitch.fakes.FakeEslConnection`) -- no real TCP
transport to `mod_event_socket` exists anywhere in this repository
(`esl.py`'s own module docstring: "an actual TCP connection ... is a real
implementation's own concern and is explicitly out of this phase's scope").
Building that transport now would be adding a telephony provider, which
Phase 2.18's own brief explicitly forbids. This process is therefore
deployable, observable (it heartbeats, and deregisters cleanly on shutdown,
exactly like every other `CallRuntime` instance `voiceagent.runtime
.reconciliation` already knows how to reason about) and correctly wired --
but it will carry zero real calls until a future phase adds the real ESL
transport and calls `runtime.start_call()` from its own event loop. This is
stated here, once, rather than left implicit.

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

Shutdown: SIGTERM or SIGINT stops accepting new work conceptually (there is
none to stop accepting yet) and calls `CallRuntime.shutdown()`, which
cancels every owned call concurrently (bounded per-call) and deregisters
this instance's heartbeat before the process exits -- a clean stop is never
mistaken for a crash by the reconciler.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket

from infra.jobs.config import get_jobs_config

from voiceagent.config import settings_from_env, validate_deployment_readiness
from voiceagent.runtime.heartbeat import RedisHeartbeatStore, new_instance_id
from voiceagent.runtime.supervisor import CallRuntime

_logger = logging.getLogger("voiceagent.scripts.run_call_runtime")


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
    _logger.info(
        "call_runtime.started",
        extra={
            "instance_id": runtime.instance_id,
            "address": address,
            "capacity": runtime.capacity,
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
    await runtime.shutdown()
    await heartbeat_store.close()
    _logger.info("call_runtime.shutdown.complete", extra={"instance_id": runtime.instance_id})


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
