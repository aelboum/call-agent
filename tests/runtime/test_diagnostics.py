"""`build_runtime_diagnostics()` (Phase 2.14 brief section 12) -- a safe
snapshot of `CallRuntime`'s own already-public surface. Reuses
`tests/runtime/test_supervisor.py`'s own fixture technique (a bare
`object()` for `CallTaskDependencies`, since `CallRuntime` never inspects
it)."""

from __future__ import annotations

import asyncio
import uuid
from typing import cast

from voiceagent.runtime.call_task import CallTaskDependencies
from voiceagent.runtime.diagnostics import build_runtime_diagnostics
from voiceagent.runtime.fakes import FakeHeartbeatStore
from voiceagent.runtime.supervisor import CallRuntime
from voiceagent.tenancy import TenantContext


def _fake_deps() -> CallTaskDependencies:
    return cast(CallTaskDependencies, object())


def _runtime() -> CallRuntime:
    return CallRuntime(
        instance_id="runtime-1",
        address="ws://runtime-1/media",
        capacity=10,
        heartbeat_store=FakeHeartbeatStore(),
    )


def _context() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), membership_id=uuid.uuid4())


def test_snapshot_of_an_idle_runtime() -> None:
    runtime = _runtime()
    snapshot = build_runtime_diagnostics(runtime)
    assert snapshot.instance_id == "runtime-1"
    assert snapshot.capacity == 10
    assert snapshot.current_load == 0
    assert snapshot.is_shutting_down is False
    assert snapshot.owned_call_session_ids == ()
    assert snapshot.has_capacity is True


def test_snapshot_reflects_current_load_and_owned_call_ids(monkeypatch) -> None:
    started = asyncio.Event()

    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        started.set()
        await asyncio.Event().wait()

    import voiceagent.runtime.supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), call_session_id, "ref-1", _fake_deps())
        await asyncio.wait_for(started.wait(), timeout=2)
        snapshot = build_runtime_diagnostics(runtime)
        assert snapshot.current_load == 1
        assert snapshot.owned_call_session_ids == (call_session_id,)
        assert snapshot.has_capacity is True

        await runtime.cancel_call(call_session_id, reason="test_cleanup")

    asyncio.run(scenario())


def test_snapshot_reflects_shutdown_state() -> None:
    runtime = _runtime()

    async def scenario() -> None:
        shutdown_task = asyncio.create_task(runtime.shutdown())
        await asyncio.sleep(0)  # let shutdown() set the flag before we snapshot
        snapshot = build_runtime_diagnostics(runtime)
        assert snapshot.is_shutting_down is True
        await shutdown_task

    asyncio.run(scenario())
