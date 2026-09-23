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


def test_close_on_a_never_used_store_is_a_safe_no_op() -> None:
    """Phase 2.16 security audit: `close()` must not require a connection to
    have ever been opened -- a route that fails before its first Redis call
    (e.g. the read itself times out) must still be able to close cleanly."""
    store = RedisHeartbeatStore("redis://127.0.0.1:1/0")
    asyncio.run(store.close())  # must not raise


def test_close_releases_and_resets_the_underlying_client() -> None:
    """Phase 2.16 security audit: `voiceagent.api.v1.ops` builds one of
    these per request and must not leak a connection -- `close()` is the
    fix; this proves it actually calls the client's own `aclose()` and
    clears `_client`, rather than being a no-op that merely looks safe."""

    class _FakeRedisClient:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    store = RedisHeartbeatStore("redis://127.0.0.1:1/0")
    fake_client = _FakeRedisClient()
    store._client = fake_client  # type: ignore[assignment]  -- simulating "already connected"

    asyncio.run(store.close())

    assert fake_client.closed is True
    assert store._client is None

    # Idempotent: closing again (no client left) must not raise.
    asyncio.run(store.close())


def test_fake_heartbeat_store_close_is_a_harmless_no_op() -> None:
    asyncio.run(FakeHeartbeatStore().close())  # must not raise
