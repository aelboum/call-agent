"""`CallRuntime`'s Phase 2.14 metrics recording -- mirrors
`tests/runtime/test_supervisor.py`'s own fixture technique exactly
(`run_call_task` monkeypatched at its import site in
`voiceagent.runtime.supervisor`)."""

from __future__ import annotations

import asyncio
import uuid
from typing import cast

import pytest

from voiceagent.runtime import supervisor as supervisor_module
from voiceagent.runtime.call_task import CallTaskDependencies
from voiceagent.runtime.fakes import FakeHeartbeatStore
from voiceagent.runtime.supervisor import CallRuntime
from voiceagent.tenancy import TenantContext


def _fake_deps() -> CallTaskDependencies:
    return cast(CallTaskDependencies, object())


def _runtime(*, cancel_timeout_seconds: float = 10.0) -> CallRuntime:
    return CallRuntime(
        instance_id="runtime-1",
        address="ws://runtime-1/media",
        capacity=10,
        heartbeat_store=FakeHeartbeatStore(),
        cancel_timeout_seconds=cancel_timeout_seconds,
    )


def _context() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), membership_id=uuid.uuid4())


@pytest.fixture
def recorded_startup_failures(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        supervisor_module,
        "record_runtime_call_startup_failure",
        lambda *, error_category: calls.append(error_category),
    )
    return calls


@pytest.fixture
def recorded_teardown_timeouts(monkeypatch) -> list[None]:
    calls: list[None] = []
    monkeypatch.setattr(supervisor_module, "record_teardown_timeout", lambda: calls.append(None))
    return calls


@pytest.fixture
def recorded_shutdown_durations(monkeypatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(
        supervisor_module, "record_runtime_shutdown", lambda duration: calls.append(duration)
    )
    return calls


def test_an_unhandled_call_task_exception_records_a_startup_failure(
    monkeypatch, recorded_startup_failures
) -> None:
    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        raise ValueError("boom")

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), call_session_id, "ref-1", _fake_deps())
        for _ in range(200):
            if not runtime.is_running(call_session_id):
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("call task never finished")

    asyncio.run(scenario())

    assert recorded_startup_failures == ["internal"]  # ValueError falls back to "internal"


def test_a_wedged_call_teardown_records_a_teardown_timeout(
    monkeypatch, recorded_teardown_timeouts
) -> None:
    """Mirrors `tests/runtime/test_supervisor.py
    ::test_cancel_call_stops_waiting_once_the_bound_elapses_for_a_wedged_teardown`'s
    own fixture exactly -- a call whose own teardown ignores cancellation."""
    started = asyncio.Event()

    async def wedged_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Simulates a provider cleanup call that itself never returns.
            await asyncio.sleep(1000)

    monkeypatch.setattr(supervisor_module, "run_call_task", wedged_run_call_task)
    runtime = _runtime(cancel_timeout_seconds=0.02)
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), call_session_id, "ref-1", _fake_deps())
        await asyncio.wait_for(started.wait(), timeout=2)
        await runtime.cancel_call(call_session_id, reason="hangup")

    asyncio.run(scenario())

    assert recorded_teardown_timeouts == [None]


def test_shutdown_records_its_own_duration(recorded_shutdown_durations) -> None:
    runtime = _runtime()
    asyncio.run(runtime.shutdown())
    assert len(recorded_shutdown_durations) == 1
    assert recorded_shutdown_durations[0] >= 0.0
