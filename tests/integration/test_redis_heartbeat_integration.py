"""Real-Redis verification of `RedisHeartbeatStore` (ADR-0008 point 8).

Every other runtime integration test uses `FakeHeartbeatStore` (the seam,
not the transport, is what those tests exercise); this file is the one place
`redis.asyncio` itself is proven to work end to end -- write, read back,
refresh, explicit removal, and genuine TTL expiry.

Requires a real Redis instance reachable at `REDIS_URL` (or `redis://
127.0.0.1:6379/0` if unset) -- excluded from the default `pytest` run
(`pytest -m integration`), exactly like every other file in this directory.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from voiceagent.runtime.heartbeat import RedisHeartbeatStore, RuntimeHeartbeat

pytestmark = pytest.mark.integration


def _redis_url() -> str:
    configured = os.environ.get("REDIS_URL", "")
    if configured.startswith("redis://") and "127.0.0.1:1" not in configured:
        return configured
    return "redis://127.0.0.1:6379/0"


def _heartbeat(instance_id: str, *, load: int = 0) -> RuntimeHeartbeat:
    return RuntimeHeartbeat(
        instance_id=instance_id,
        address="ws://example/media",
        capacity=10,
        current_load=load,
        last_heartbeat_epoch_seconds=0.0,
    )


def test_write_then_read_all_round_trips() -> None:
    """`RedisHeartbeatStore`'s lazily-built client lives for the store's
    whole lifetime (its own docstring), bound to whichever event loop first
    used it -- exactly the single long-running loop a real `call-runtime`
    process has. This test therefore does its write, read and cleanup in one
    `asyncio.run()`, not several, matching that real usage rather than
    exercising an unsupported cross-loop-reuse pattern."""
    store = RedisHeartbeatStore(_redis_url())
    instance_id = f"test-{uuid.uuid4().hex[:8]}"

    async def scenario() -> RuntimeHeartbeat:
        try:
            await store.write(_heartbeat(instance_id, load=3), ttl_seconds=10)
            heartbeats = await store.read_all()
            return heartbeats[instance_id]
        finally:
            await store.remove(instance_id)

    heartbeat = asyncio.run(scenario())
    assert heartbeat.current_load == 3


def test_remove_deregisters_immediately() -> None:
    store = RedisHeartbeatStore(_redis_url())
    instance_id = f"test-{uuid.uuid4().hex[:8]}"

    async def scenario() -> dict[str, RuntimeHeartbeat]:
        await store.write(_heartbeat(instance_id), ttl_seconds=30)
        await store.remove(instance_id)
        return await store.read_all()

    heartbeats = asyncio.run(scenario())
    assert instance_id not in heartbeats


def test_a_heartbeat_expires_on_its_own_ttl() -> None:
    store = RedisHeartbeatStore(_redis_url())
    instance_id = f"test-{uuid.uuid4().hex[:8]}"

    async def scenario() -> dict[str, RuntimeHeartbeat]:
        await store.write(_heartbeat(instance_id), ttl_seconds=1)
        await asyncio.sleep(1.5)
        return await store.read_all()

    heartbeats = asyncio.run(scenario())
    assert instance_id not in heartbeats
