"""Runtime liveness via Redis (ADR-0008 point 8), not a database table.

`infra.db` is sync-only (Phase 0 report §2.4 G-1) and liveness is inherently
ephemeral, transient state -- exactly what `infra.jobs`'s own Redis usage
(dead-letter tracking) already models. Each `call-runtime` process writes
`runtime:{instance_id} -> {address, capacity, current_load, last_heartbeat}`
on a short TTL, refreshed continuously; the Call Orchestrator
(`voiceagent.runtime.assignment`) reads this set to pick an assignee, and the
reconciliation loop (`voiceagent.runtime.reconciliation`) reads it to detect
staleness.

`HeartbeatStore` is the product-owned seam: `RedisHeartbeatStore` is the one
real implementation, built lazily over `redis.asyncio` (never at import
time -- `tests/architecture/test_import_side_effects.py` covers this module),
and `voiceagent.runtime.fakes.FakeHeartbeatStore` is the deterministic
in-memory double every hermetic test uses instead. Redis is liveness/
coordination only: PostgreSQL (`call_sessions`) remains the authoritative,
durable store for which runtime a call is assigned to (Phase 2.0 report §8) --
nothing here is ever treated as the source of truth for a call's own state.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "HeartbeatStore",
    "RedisHeartbeatStore",
    "RuntimeHeartbeat",
    "new_instance_id",
    "wall_clock_now",
]


@dataclass(frozen=True, slots=True)
class RuntimeHeartbeat:
    """One `call-runtime` process's self-reported liveness and load.

    `last_heartbeat_epoch_seconds` is wall-clock (`time.time()`), not
    monotonic: it is written by one process and read by others (the
    orchestrator, the reconciler), so a monotonic clock -- whose epoch is
    process-local and meaningless across processes -- would compare
    incommensurable values. It is informational only (observability/display);
    the mechanism this design actually relies on for staleness is Redis key
    *expiry* (ADR-0008 point 9), not comparing this timestamp to "now" --
    `HeartbeatStore.read_all()` simply never returns an expired key.
    """

    instance_id: str
    address: str
    capacity: int
    current_load: int
    last_heartbeat_epoch_seconds: float

    @property
    def has_capacity(self) -> bool:
        return self.current_load < self.capacity


@runtime_checkable
class HeartbeatStore(Protocol):
    """Runtime liveness registration and discovery."""

    async def write(self, heartbeat: RuntimeHeartbeat, *, ttl_seconds: float) -> None:
        """Write (or refresh) one runtime's heartbeat, on the given TTL."""
        ...

    async def read_all(self) -> dict[str, RuntimeHeartbeat]:
        """Every currently-live heartbeat, keyed by `instance_id`. A runtime
        whose key has expired is simply absent -- there is no "expired but
        still listed" state to check for separately."""
        ...

    async def remove(self, instance_id: str) -> None:
        """Explicit deregistration (graceful shutdown). Idempotent."""
        ...


_KEY_PREFIX = "voiceagent:runtime:"


class RedisHeartbeatStore:
    """`HeartbeatStore` over `redis.asyncio`. The client is constructed
    lazily on first use, never at import time or `__init__` time -- so
    importing this module, and constructing a store with no operation yet
    performed on it, opens no socket (the same invariant
    `voiceagent.tenancy`/`voiceagent.db` already hold for the synchronous
    database seam)."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client = None

    def _get_client(self):
        if self._client is None:
            import redis.asyncio as redis

            self._client = redis.Redis.from_url(self._redis_url, decode_responses=True)
        return self._client

    @staticmethod
    def _key(instance_id: str) -> str:
        return f"{_KEY_PREFIX}{instance_id}"

    async def write(self, heartbeat: RuntimeHeartbeat, *, ttl_seconds: float) -> None:
        client = self._get_client()
        payload = json.dumps(
            {
                "instance_id": heartbeat.instance_id,
                "address": heartbeat.address,
                "capacity": heartbeat.capacity,
                "current_load": heartbeat.current_load,
                "last_heartbeat_epoch_seconds": heartbeat.last_heartbeat_epoch_seconds,
            }
        )
        await client.set(self._key(heartbeat.instance_id), payload, ex=round(ttl_seconds) or 1)

    async def read_all(self) -> dict[str, RuntimeHeartbeat]:
        client = self._get_client()
        heartbeats: dict[str, RuntimeHeartbeat] = {}
        async for key in client.scan_iter(match=f"{_KEY_PREFIX}*"):
            raw = await client.get(key)
            if raw is None:
                continue  # expired between SCAN and GET -- not an error.
            data = json.loads(raw)
            heartbeats[data["instance_id"]] = RuntimeHeartbeat(**data)
        return heartbeats

    async def remove(self, instance_id: str) -> None:
        client = self._get_client()
        await client.delete(self._key(instance_id))


def new_instance_id() -> str:
    """A stable-for-the-process, opaque runtime identity (ADR-0008 point 4:
    "hostname + a random suffix is sufficient; it is an opaque string to
    everything except the reconciler"). Never parsed for meaning anywhere in
    the product -- only compared for equality."""
    import socket

    host = socket.gethostname()
    return f"{host}-{uuid.uuid4().hex[:8]}"


def wall_clock_now() -> float:
    return time.time()
