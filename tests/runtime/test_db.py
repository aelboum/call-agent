"""`DatabaseBoundary` (Phase 2.2 brief section 16): a slow synchronous call
crossing the boundary must not block other work on the same event loop --
the dynamic proof that the media/audio path stays responsive while a
database call is in flight on its own bounded thread pool."""

from __future__ import annotations

import asyncio
import time

from voiceagent.runtime.db import DatabaseBoundary


def _blocking_sleep(seconds: float) -> str:
    time.sleep(seconds)
    return "done"


def test_run_offloads_a_blocking_call_and_returns_its_result() -> None:
    boundary = DatabaseBoundary(max_workers=2)

    async def scenario() -> str:
        return await boundary.run(_blocking_sleep, 0.05)

    assert asyncio.run(scenario()) == "done"
    boundary.close()


def test_the_event_loop_is_not_blocked_while_a_call_is_in_flight() -> None:
    """A concurrent, cheap coroutine must complete *before* the slow
    boundary call does, proving the blocking work truly left the loop."""
    boundary = DatabaseBoundary(max_workers=2)
    order: list[str] = []

    async def slow() -> None:
        await boundary.run(_blocking_sleep, 0.1)
        order.append("slow")

    async def fast() -> None:
        await asyncio.sleep(0.01)
        order.append("fast")

    async def scenario() -> None:
        await asyncio.gather(slow(), fast())

    asyncio.run(scenario())
    boundary.close()
    assert order == ["fast", "slow"]


def test_pool_size_bounds_concurrent_blocking_work() -> None:
    """With `max_workers=1`, two blocking calls cannot overlap -- the second
    must wait for the thread pool's one worker, proving the pool is a real,
    sized resource and not an unbounded implicit executor."""
    boundary = DatabaseBoundary(max_workers=1)
    active = 0
    max_active = 0

    def track(seconds: float) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        time.sleep(seconds)
        active -= 1

    async def scenario() -> None:
        await asyncio.gather(
            boundary.run(track, 0.05),
            boundary.run(track, 0.05),
        )

    asyncio.run(scenario())
    boundary.close()
    assert max_active == 1


def test_close_is_idempotent() -> None:
    boundary = DatabaseBoundary(max_workers=1)
    boundary.close()
    boundary.close()
