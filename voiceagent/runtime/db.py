"""The one seam through which call-hosting, audio-adjacent runtime code may
reach the database (Phase 0 report §2.4 G-1; ADR-0008 point 2; this phase's
brief section 16 -- "the media/audio loop must NEVER perform synchronous
database I/O").

`infra.db` exposes no async engine, no `AsyncSession`, no `asyncpg`
(reconfirmed against the pinned SaaS-OS commit). Every database or SaaS-OS
touch from `voiceagent.runtime.call_task`/`voiceagent.runtime.supervisor`
therefore crosses `DatabaseBoundary.run()`, which schedules the synchronous
call onto a bounded, explicitly-sized thread pool -- never the bare
`asyncio.to_thread()` default executor, so the pool's size is a visible,
configured resource (`voiceagent.config.settings.RuntimeSettings
.to_thread_pool_size`) rather than an implicit shared one another part of the
process could also be saturating.

**Only at call-start, at a tool-call boundary, and at call-end** (ADR-0008
point 2) -- never inside the frame-by-frame audio pump itself
(`voiceagent.runtime.call_task`'s `_pump_*` coroutines never call this
module). `tests/architecture/test_runtime_db_boundary.py` asserts the
audio-pump module imports neither `voiceagent.db` nor `voiceagent.tenancy`
nor `voiceagent.calls.service`/`voiceagent.agents.service` directly -- only
through a `DatabaseBoundary` passed in.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

__all__ = ["DatabaseBoundary"]

_P = ParamSpec("_P")
_T = TypeVar("_T")


class DatabaseBoundary:
    """Wraps a bounded `ThreadPoolExecutor`. `run(fn, *args, **kwargs)` is
    the only way call-runtime code may invoke a synchronous database/SaaS-OS
    function; nothing about this class is specific to any one such function,
    so the same boundary serves `voiceagent.calls.service`,
    `voiceagent.agents.service`, and `control_plane.data_authorization
    .authorize_data_access()` alike."""

    def __init__(self, *, max_workers: int) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="voiceagent-runtime-db"
        )

    async def run(self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args, **kwargs))

    def close(self) -> None:
        """Idempotent. Does not wait for in-flight work and cancels
        not-yet-started work -- a runtime shutdown must not hang on a slow
        query for a call that is being torn down anyway."""
        self._executor.shutdown(wait=False, cancel_futures=True)
