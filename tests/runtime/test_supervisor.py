"""`CallRuntime` (`voiceagent.runtime.supervisor`) -- hermetic, no database,
no real `run_call_task()`. Every test monkeypatches
`voiceagent.runtime.supervisor.run_call_task` with a small scripted
coroutine, exactly the same "swap the one hardcoded dependency at its import
site" technique `tests/runtime/test_reconciliation.py` and friends already
use elsewhere in this codebase for a module-level function that has no
constructor-injected seam of its own. `CallTaskDependencies` itself is never
actually consulted by these fakes, so a bare `object()` stands in for it --
`CallRuntime` never inspects `deps`, only threads it through to
`run_call_task()`.

Phase 2.13 hardening this file exists to prove, none of it previously
covered by any test (brief §21's own "Runtime ownership: owner accepted,
non-owner rejected, ... shutdown behavior" and "Shutdown: active calls,
multiple active calls, stuck provider, bounded shutdown, isolated call
failure" rows):

* `cancel_call()`/`shutdown()` are bounded -- a call whose own teardown
  ignores cancellation cannot block either forever.
* `shutdown()` cancels every owned call concurrently, not one at a time.
* one call's failure/timeout is isolated from every other call's.
* `start_call()` stays idempotent, and `error_for()`/`is_running()` reflect
  each call independently.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import cast

import pytest

from voiceagent.runtime import supervisor as supervisor_module
from voiceagent.runtime.call_task import CallTaskDependencies
from voiceagent.runtime.fakes import FakeHeartbeatStore
from voiceagent.runtime.heartbeat import RuntimeHeartbeat
from voiceagent.runtime.supervisor import CallRuntime
from voiceagent.tenancy import TenantContext


def _fake_deps() -> CallTaskDependencies:
    """`CallRuntime` never inspects `deps` -- see module docstring -- so a
    bare `object()`, cast for `pyright`'s benefit, stands in for it in every
    test here."""
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


def test_start_call_is_idempotent_for_an_already_running_call(monkeypatch) -> None:
    starts = 0
    started = asyncio.Event()

    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        nonlocal starts
        starts += 1
        started.set()
        await asyncio.Event().wait()  # never returns on its own.

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), call_session_id, "ref-1", _fake_deps())
        await asyncio.wait_for(started.wait(), timeout=2)
        # A redelivered assignment for the same call must not start a
        # second, duplicate task (module docstring).
        await runtime.start_call(_context(), call_session_id, "ref-1", _fake_deps())
        assert runtime.current_load == 1
        await runtime.cancel_call(call_session_id, reason="hangup")

    asyncio.run(scenario())
    assert starts == 1


def test_cancel_call_is_a_no_op_for_unknown_or_finished_call() -> None:
    runtime = _runtime()

    async def scenario() -> None:
        # Never started at all.
        await runtime.cancel_call(uuid.uuid4(), reason="hangup")

    asyncio.run(scenario())  # must not raise


def test_cancel_call_delivers_the_reason_and_waits_for_a_well_behaved_teardown(
    monkeypatch,
) -> None:
    observed_reason: str | None = None
    started = asyncio.Event()

    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        nonlocal observed_reason
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            observed_reason = cancellation.reason
            raise

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), call_session_id, "ref-1", _fake_deps())
        await asyncio.wait_for(started.wait(), timeout=2)
        await runtime.cancel_call(call_session_id, reason="provider_disconnect")

    asyncio.run(scenario())
    assert observed_reason == "provider_disconnect"
    assert runtime.is_running(call_session_id) is False


def test_cancel_call_stops_waiting_once_the_bound_elapses_for_a_wedged_teardown(
    monkeypatch,
) -> None:
    """A call whose own teardown ignores cancellation (brief §16: "one
    broken call must not block shutdown forever") must not be able to make
    `cancel_call()` wait forever -- it gives up waiting at
    `cancel_timeout_seconds` while the task itself keeps running in the
    background."""
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def wedged_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            # Simulates a provider cleanup call that itself never returns --
            # deliberately does NOT re-raise or return promptly.
            await asyncio.sleep(1000)

    monkeypatch.setattr(supervisor_module, "run_call_task", wedged_run_call_task)
    runtime = _runtime(cancel_timeout_seconds=0.05)
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), call_session_id, "ref-1", _fake_deps())
        await asyncio.wait_for(started.wait(), timeout=2)
        started_at = time.monotonic()
        await runtime.cancel_call(call_session_id, reason="runtime_shutdown")
        elapsed = time.monotonic() - started_at
        assert elapsed < 1.0, "cancel_call() waited far longer than its own bound"
        await asyncio.wait_for(cancellation_seen.wait(), timeout=2)
        # The wedged task is still alive in the background -- cancel_call()
        # gave up *waiting*, it never force-kills the task a second way.
        assert runtime.is_running(call_session_id) is True

    asyncio.run(scenario())


def test_shutdown_cancels_every_call_concurrently_not_sequentially(monkeypatch) -> None:
    """Two calls, each taking ~0.1s to tear down after cancellation:
    sequential cancellation would make `shutdown()` take ~0.2s; concurrent
    cancellation (brief §16) keeps it near 0.1s regardless of how many calls
    are owned."""
    per_call_teardown_seconds = 0.1
    started = {}

    async def slow_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        started[call_session_id] = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(per_call_teardown_seconds)
            raise

    monkeypatch.setattr(supervisor_module, "run_call_task", slow_run_call_task)
    runtime = _runtime(cancel_timeout_seconds=5.0)
    call_ids = [uuid.uuid4() for _ in range(4)]

    async def scenario() -> float:
        for call_id in call_ids:
            await runtime.start_call(_context(), call_id, f"ref-{call_id}", _fake_deps())
        for _ in range(200):
            if len(started) == len(call_ids):
                break
            await asyncio.sleep(0.005)
        started_at = time.monotonic()
        await runtime.shutdown()
        return time.monotonic() - started_at

    elapsed = asyncio.run(scenario())
    # Comfortably under the sequential bound (4 * 0.1s = 0.4s), and close to
    # one call's own teardown time.
    assert elapsed < per_call_teardown_seconds * 2.5, elapsed
    for call_id in call_ids:
        assert runtime.is_running(call_id) is False


def test_shutdown_deregisters_the_heartbeat_even_with_a_stuck_call(monkeypatch) -> None:
    started = asyncio.Event()

    async def wedged_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(1000)

    monkeypatch.setattr(supervisor_module, "run_call_task", wedged_run_call_task)
    store = FakeHeartbeatStore()
    runtime = CallRuntime(
        instance_id="runtime-stuck",
        address="ws://runtime-stuck/media",
        capacity=10,
        heartbeat_store=store,
        cancel_timeout_seconds=0.05,
    )

    async def scenario() -> tuple[bool, dict[str, RuntimeHeartbeat]]:
        await runtime.start_call(_context(), uuid.uuid4(), "ref-1", _fake_deps())
        await asyncio.wait_for(started.wait(), timeout=2)
        # Written directly (rather than relying on the heartbeat loop's own
        # timing) so this test proves *removal*, deterministically, without
        # racing `_run_heartbeat_loop()`'s first write.
        await store.write(
            RuntimeHeartbeat(
                instance_id="runtime-stuck",
                address="ws://runtime-stuck/media",
                capacity=10,
                current_load=1,
                last_heartbeat_epoch_seconds=0.0,
            ),
            ttl_seconds=1000,
        )
        was_present = "runtime-stuck" in await store.read_all()
        await asyncio.wait_for(runtime.shutdown(), timeout=2)
        return was_present, await store.read_all()

    was_present, live_after = asyncio.run(scenario())
    assert was_present is True
    assert "runtime-stuck" not in live_after


def test_one_calls_failure_is_isolated_from_another_calls_state(monkeypatch) -> None:
    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        if call_ref == "ref-broken":
            raise RuntimeError("provider blew up")
        await asyncio.sleep(0.02)

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    broken_id = uuid.uuid4()
    healthy_id = uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), broken_id, "ref-broken", _fake_deps())
        await runtime.start_call(_context(), healthy_id, "ref-healthy", _fake_deps())
        for _ in range(200):
            if not runtime.is_running(broken_id) and not runtime.is_running(healthy_id):
                break
            await asyncio.sleep(0.005)

    asyncio.run(scenario())
    assert runtime.error_for(broken_id) is not None
    assert isinstance(runtime.error_for(broken_id), RuntimeError)
    assert runtime.error_for(healthy_id) is None


@pytest.mark.parametrize("reason", ["hangup", "runtime_shutdown", "provider_disconnect"])
def test_cancel_call_reason_reaches_the_cancellation_signal_for_every_reason(
    monkeypatch, reason: str
) -> None:
    seen: list[str] = []
    started = asyncio.Event()

    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seen.append(cancellation.reason)
            raise

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), call_session_id, "ref-1", _fake_deps())
        await asyncio.wait_for(started.wait(), timeout=2)
        await runtime.cancel_call(call_session_id, reason=reason)

    asyncio.run(scenario())
    assert seen == [reason]
