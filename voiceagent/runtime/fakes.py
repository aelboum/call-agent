"""In-memory runtime fakes, for hermetic tests (no Redis, no real clock
dependency the test cannot control).

Mirrors `voiceagent.telephony.fakes`'s own rationale: these are fakes, not
mocks, with real (if simplified) TTL-expiry behavior, so a test that passes
against `FakeHeartbeatStore` exercises the same staleness-detection logic a
real `RedisHeartbeatStore` would.
"""

from __future__ import annotations

from voiceagent.runtime.heartbeat import RuntimeHeartbeat

__all__ = ["FakeHeartbeatStore"]


class FakeHeartbeatStore:
    """A `HeartbeatStore` over a plain dict, with an injectable clock so a
    test can advance time deterministically to simulate TTL expiry without
    an actual `sleep()`."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[RuntimeHeartbeat, float]] = {}
        self.now: float = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def write(self, heartbeat: RuntimeHeartbeat, *, ttl_seconds: float) -> None:
        self._entries[heartbeat.instance_id] = (heartbeat, self.now + ttl_seconds)

    async def read_all(self) -> dict[str, RuntimeHeartbeat]:
        live = {
            instance_id: heartbeat
            for instance_id, (heartbeat, expires_at) in self._entries.items()
            if expires_at > self.now
        }
        expired = self._entries.keys() - live.keys()
        for instance_id in expired:
            del self._entries[instance_id]
        return live

    async def remove(self, instance_id: str) -> None:
        self._entries.pop(instance_id, None)

    async def close(self) -> None:
        """No real connection to release -- present only so this fake keeps
        satisfying the `HeartbeatStore` protocol."""
