"""`HeartbeatStore` (ADR-0008 point 8): TTL-expiry semantics against
`FakeHeartbeatStore`, and that constructing/importing `RedisHeartbeatStore`
opens no connection (its client is built lazily, on first real use)."""

from __future__ import annotations

import asyncio

from voiceagent.runtime.fakes import FakeHeartbeatStore
from voiceagent.runtime.heartbeat import HeartbeatStore, RedisHeartbeatStore, RuntimeHeartbeat


def _heartbeat(
    instance_id: str = "runtime-1", *, load: int = 0, capacity: int = 10
) -> RuntimeHeartbeat:
    return RuntimeHeartbeat(
        instance_id=instance_id,
        address="ws://runtime-1/media",
        capacity=capacity,
        current_load=load,
        last_heartbeat_epoch_seconds=0.0,
    )


def test_fake_store_satisfies_the_protocol() -> None:
    assert isinstance(FakeHeartbeatStore(), HeartbeatStore)


def test_write_then_read_all_returns_the_heartbeat() -> None:
    store = FakeHeartbeatStore()

    async def scenario() -> dict[str, RuntimeHeartbeat]:
        await store.write(_heartbeat(), ttl_seconds=10)
        return await store.read_all()

    heartbeats = asyncio.run(scenario())
    assert set(heartbeats) == {"runtime-1"}
    assert heartbeats["runtime-1"].current_load == 0


def test_expired_heartbeat_is_absent_from_read_all() -> None:
    store = FakeHeartbeatStore()

    async def scenario() -> dict[str, RuntimeHeartbeat]:
        await store.write(_heartbeat(), ttl_seconds=5)
        store.advance(6)
        return await store.read_all()

    assert asyncio.run(scenario()) == {}


def test_refreshing_a_heartbeat_before_expiry_keeps_it_live() -> None:
    store = FakeHeartbeatStore()

    async def scenario() -> dict[str, RuntimeHeartbeat]:
        await store.write(_heartbeat(load=1), ttl_seconds=5)
        store.advance(3)
        await store.write(_heartbeat(load=2), ttl_seconds=5)
        store.advance(3)  # 6s since the refresh, but only 3s since re-write
        return await store.read_all()

    heartbeats = asyncio.run(scenario())
    assert heartbeats["runtime-1"].current_load == 2


def test_remove_deregisters_immediately() -> None:
    store = FakeHeartbeatStore()

    async def scenario() -> dict[str, RuntimeHeartbeat]:
        await store.write(_heartbeat(), ttl_seconds=100)
        await store.remove("runtime-1")
        return await store.read_all()

    assert asyncio.run(scenario()) == {}


def test_has_capacity() -> None:
    assert _heartbeat(load=3, capacity=10).has_capacity
    assert not _heartbeat(load=10, capacity=10).has_capacity


def test_redis_store_construction_opens_no_connection() -> None:
    """The `redis.asyncio.Redis` client is built lazily on first `run()`, not
    at `__init__` -- constructing a `RedisHeartbeatStore` against an
    unreachable URL must not raise or block."""
    store = RedisHeartbeatStore("redis://127.0.0.1:1/0")
    assert isinstance(store, HeartbeatStore)
