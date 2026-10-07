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


async def _run_to_completion(
    runtime: CallRuntime, call_session_id: uuid.UUID, *, timeout: float = 2.0
) -> None:
    """Poll until `call_session_id` is no longer running. Every fake
    `run_call_task` in this module either raises or returns immediately (no
    real I/O), so this only ever waits out genuine event-loop scheduling,
    never wall-clock call duration."""
    deadline = time.monotonic() + timeout
    while runtime.is_running(call_session_id):
        if time.monotonic() > deadline:
            raise AssertionError(f"call {call_session_id} never finished")
        await asyncio.sleep(0.005)


def test_error_for_is_reaped_once_a_different_call_starts(monkeypatch) -> None:
    """Phase 2.30 remediation (brief section 4, 'Failed call cleanup'): a
    failed call's `_errors` entry does not remain indefinitely -- it survives
    exactly until a *different* call starts on this runtime, at which point
    `start_call()`'s own `_reap_terminal_errors()` evicts it. `_tasks`/
    `_cancellations` cleanup (already proven elsewhere in this file) is
    unaffected -- this test only concerns `_errors`."""

    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        if call_ref == "ref-broken":
            raise RuntimeError("provider blew up")

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    broken_id = uuid.uuid4()
    next_id = uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), broken_id, "ref-broken", _fake_deps())
        await _run_to_completion(runtime, broken_id)
        assert runtime.error_for(broken_id) is not None

        await runtime.start_call(_context(), next_id, "ref-next", _fake_deps())
        # The reap happens synchronously inside `start_call()`, before the
        # new task is even scheduled -- no need to wait for `next_id` itself
        # to finish before observing `broken_id`'s entry is gone.
        assert runtime.error_for(broken_id) is None
        await _run_to_completion(runtime, next_id)

    asyncio.run(scenario())


def test_successful_call_leaves_no_error_state(monkeypatch) -> None:
    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        return

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    call_id = uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), call_id, "ref-ok", _fake_deps())
        await _run_to_completion(runtime, call_id)

    asyncio.run(scenario())
    assert runtime.error_for(call_id) is None


def test_cancelled_call_leaves_no_error_state(monkeypatch) -> None:
    started = asyncio.Event()

    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    call_id = uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), call_id, "ref-cancel", _fake_deps())
        await asyncio.wait_for(started.wait(), timeout=2)
        await runtime.cancel_call(call_id, reason="hangup")

    asyncio.run(scenario())
    assert runtime.is_running(call_id) is False
    assert runtime.error_for(call_id) is None


def test_repeated_failures_do_not_accumulate_in_the_errors_dict(monkeypatch) -> None:
    """Reproduces, hermetically, the exact defect Phase 2.30's real-staging
    soak measured (`docs/PHASE-2.30-LONG-RUNNING-STABILITY.md` section 1):
    before this remediation, `len(CallRuntime._errors)` grew by one for
    every failed call and never shrank -- N failed calls in, N entries
    retained, for the life of the process. After this remediation, the dict
    never holds more than the single most recently failed call's entry,
    because each new call's own `start_call()` reaps the previous one."""

    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        raise RuntimeError("provider blew up")

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    observed_sizes: list[int] = []

    async def scenario() -> None:
        for _ in range(10):
            call_id = uuid.uuid4()
            await runtime.start_call(_context(), call_id, "ref-fail", _fake_deps())
            await _run_to_completion(runtime, call_id)
            assert runtime.error_for(call_id) is not None
            observed_sizes.append(len(runtime._errors))  # noqa: SLF001 -- assert the old defect stays fixed

    asyncio.run(scenario())
    # Old (buggy) behavior would have produced [1, 2, 3, ..., 10]: strictly
    # monotonic, unbounded growth. Fixed behavior: every entry is its own
    # call's, and none has been reaped yet by the *time it is observed*
    # (only the next call's start reaps the *previous* one) -- so each
    # observation here is exactly 1, never accumulating.
    assert observed_sizes == [1] * 10


def test_concurrent_calls_error_cleanup_is_isolated_and_bounded(monkeypatch) -> None:
    """Brief section 3/4 ('concurrent calls'): a failed call's reaping must
    never touch another call's still-relevant state, whether that other
    call is currently running, already succeeded, or is itself a distinct
    failure -- and reaping is still eventually bounded once new,
    unconnected call activity begins."""
    release_b = asyncio.Event()

    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        if call_ref == "ref-a":
            raise RuntimeError("A blew up")
        if call_ref == "ref-b":
            await release_b.wait()
            return
        if call_ref == "ref-c":
            return
        raise AssertionError(f"unexpected call_ref {call_ref!r}")

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    call_a, call_b, call_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def scenario() -> None:
        # A and B start concurrently -- A fails almost immediately while B
        # is still deliberately held open, so A's reap-on-next-start cannot
        # have fired yet (no *other* call has started since A's own start).
        await runtime.start_call(_context(), call_a, "ref-a", _fake_deps())
        await runtime.start_call(_context(), call_b, "ref-b", _fake_deps())
        await _run_to_completion(runtime, call_a)
        assert runtime.error_for(call_a) is not None
        assert runtime.is_running(call_b) is True

        release_b.set()
        await _run_to_completion(runtime, call_b)
        # B succeeding, and its own start_call() having already run before
        # A failed, must not have disturbed A's error.
        assert runtime.error_for(call_a) is not None
        assert runtime.error_for(call_b) is None

        # A brand new, unrelated call C starting is what finally reaps A --
        # and must never remove B's (already-`None`) or its own state.
        await runtime.start_call(_context(), call_c, "ref-c", _fake_deps())
        assert runtime.error_for(call_a) is None
        assert runtime.error_for(call_b) is None
        await _run_to_completion(runtime, call_c)
        assert runtime.error_for(call_c) is None

    asyncio.run(scenario())


def test_error_metric_and_log_are_recorded_independently_of_errors_dict_reaping(
    monkeypatch,
) -> None:
    """Brief section 4 ('Error observability'): reaping `_errors` must not
    remove the failure information from whatever durable/logging path is
    supposed to retain it. `CallRuntime` itself durably records a failure
    two ways independent of `_errors` -- `_logger.exception(...)` and
    `record_runtime_call_startup_failure()` (a metric) -- both fire from
    `_wrap_call_task()`'s `except` clause, before `_errors` is ever reaped,
    and neither is affected by a later call's `start_call()` reaping the
    dict entry."""
    recorded_categories: list[str] = []

    def fake_record_failure(*, error_category: str) -> None:
        recorded_categories.append(error_category)

    monkeypatch.setattr(
        supervisor_module, "record_runtime_call_startup_failure", fake_record_failure
    )

    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        raise RuntimeError("provider blew up")

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    call_a, call_b = uuid.uuid4(), uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), call_a, "ref-a", _fake_deps())
        await _run_to_completion(runtime, call_a)
        # Reaps call_a's `_errors` entry -- the metric was already recorded
        # before this point and must not be affected by the reap.
        await runtime.start_call(_context(), call_b, "ref-b", _fake_deps())
        await _run_to_completion(runtime, call_b)

    asyncio.run(scenario())
    assert runtime.error_for(call_a) is None
    assert recorded_categories == ["internal", "internal"]


def test_mixed_concurrent_success_failure_cancel_cleanup_is_isolated(monkeypatch) -> None:
    """Phase 2.31 (brief section C/F): Phase 2.30's own concurrent-cleanup
    test only ever mixed a failure with a success. This proves the same
    isolation property holds with all three terminal shapes -- success,
    failure, and cancellation -- live on this runtime at once, matching
    this phase's own real-staging concurrent trials (normal + media_fail +
    cancel in one group). After all three settle: `_tasks` and
    `_cancellations` are both back to empty (every call's own bookkeeping
    is fully released, regardless of how it ended), the failed call's
    error is observable, and neither the successful nor the cancelled
    call ever populated `_errors`. A fourth, unrelated call starting is
    what reaps the failed call's entry, touching nothing else."""
    release_success = asyncio.Event()
    cancel_started = asyncio.Event()

    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        if call_ref == "ref-fail":
            raise RuntimeError("provider blew up")
        if call_ref == "ref-success":
            await release_success.wait()
            return
        if call_ref == "ref-cancel":
            cancel_started.set()
            await asyncio.Event().wait()
        raise AssertionError(f"unexpected call_ref {call_ref!r}")

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    call_fail, call_success, call_cancel, call_next = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )

    async def scenario() -> None:
        await runtime.start_call(_context(), call_fail, "ref-fail", _fake_deps())
        await runtime.start_call(_context(), call_success, "ref-success", _fake_deps())
        await runtime.start_call(_context(), call_cancel, "ref-cancel", _fake_deps())
        await asyncio.wait_for(cancel_started.wait(), timeout=2)

        await _run_to_completion(runtime, call_fail)
        assert runtime.error_for(call_fail) is not None
        assert runtime.is_running(call_success) is True
        assert runtime.is_running(call_cancel) is True

        release_success.set()
        await _run_to_completion(runtime, call_success)
        await runtime.cancel_call(call_cancel, reason="hangup")

        assert runtime._tasks == {}  # noqa: SLF001 -- every call's own bookkeeping released
        assert runtime._cancellations == {}  # noqa: SLF001
        assert runtime.error_for(call_fail) is not None
        assert runtime.error_for(call_success) is None
        assert runtime.error_for(call_cancel) is None

        await runtime.start_call(_context(), call_next, "ref-success", _fake_deps())
        assert runtime.error_for(call_fail) is None
        assert runtime.error_for(call_success) is None
        assert runtime.error_for(call_cancel) is None
        release_success.set()
        await _run_to_completion(runtime, call_next)

    asyncio.run(scenario())


def test_long_mixed_failure_recovery_sequence_returns_bookkeeping_to_baseline(
    monkeypatch,
) -> None:
    """Phase 2.31 (brief section B): Phase 2.30's own repeated-failure test
    (`test_repeated_failures_do_not_accumulate_in_the_errors_dict`) only
    ever exercised failures. This runs the brief's own suggested mixed
    pattern -- normal / media-failure-analog / cancellation, repeated,
    including back-to-back failures -- sequentially on one long-lived
    runtime, and proves `_tasks` and `_cancellations` return to their
    empty baseline after every single call regardless of which of the
    three ways it ended, that `_errors` never holds more than one entry
    at a time, and that no call's own outcome is influenced by whatever
    came immediately before it."""
    cancel_started = asyncio.Event()

    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        if call_ref == "fail":
            raise RuntimeError("provider blew up")
        if call_ref == "cancel":
            cancel_started.set()
            await asyncio.Event().wait()
        return  # "normal"

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    runtime = _runtime()
    # normal, failure, normal, failure, cancel, normal, failure, failure,
    # normal, cancel -- mirrors this phase's own real-staging pattern
    # (brief section B), compressed to run hermetically in milliseconds.
    sequence = [
        "normal",
        "fail",
        "normal",
        "fail",
        "cancel",
        "normal",
        "fail",
        "fail",
        "normal",
        "cancel",
    ]
    max_errors_dict_size = 0

    async def scenario() -> None:
        nonlocal max_errors_dict_size
        for kind in sequence:
            call_id = uuid.uuid4()
            cancel_started.clear()
            await runtime.start_call(_context(), call_id, kind, _fake_deps())
            if kind == "cancel":
                await asyncio.wait_for(cancel_started.wait(), timeout=2)
                await runtime.cancel_call(call_id, reason="hangup")
            else:
                await _run_to_completion(runtime, call_id)
            assert runtime._tasks == {}  # noqa: SLF001 -- baseline after every call, any outcome
            assert runtime._cancellations == {}  # noqa: SLF001
            max_errors_dict_size = max(max_errors_dict_size, len(runtime._errors))  # noqa: SLF001
            if kind == "fail":
                assert runtime.error_for(call_id) is not None
            else:
                assert runtime.error_for(call_id) is None

    asyncio.run(scenario())
    # Never more than the single most-recently-failed call's entry, no
    # matter how many failures (4, including two back-to-back) occurred
    # across the whole sequence -- the exact invariant Phase 2.30's fix
    # established, now proven under a genuinely mixed workload too.
    assert max_errors_dict_size <= 1


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


class _ScriptedHeartbeatStore(FakeHeartbeatStore):
    """A `FakeHeartbeatStore` whose `write()` raises for a scripted prefix of
    calls (simulating a transient Redis outage -- connection reset, timeout,
    whatever) before falling through to the real in-memory write every call
    after the script is exhausted. Proves Phase 2.38's own retry/recovery
    behavior without needing a real Redis client."""

    def __init__(self, write_outcomes: list[Exception | None]) -> None:
        super().__init__()
        self._outcomes = list(write_outcomes)
        self.write_calls = 0

    async def write(self, heartbeat: RuntimeHeartbeat, *, ttl_seconds: float) -> None:
        self.write_calls += 1
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if outcome is not None:
                raise outcome
        await super().write(heartbeat, ttl_seconds=ttl_seconds)


async def _wait_for_write_calls(store: _ScriptedHeartbeatStore, at_least: int) -> None:
    for _ in range(400):
        if store.write_calls >= at_least:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"write_calls never reached {at_least}, stuck at {store.write_calls}")


def test_heartbeat_survives_one_transient_redis_failure(monkeypatch) -> None:
    """Test 1 (Phase 2.38 brief): a single failed `write()` must not kill the
    heartbeat task -- the next tick's write succeeds normally."""
    store = _ScriptedHeartbeatStore([ConnectionError("redis unavailable")])
    runtime = CallRuntime(
        instance_id="runtime-hb-1",
        address="ws://runtime-hb-1/media",
        capacity=10,
        heartbeat_store=store,
    )

    async def scenario() -> None:
        runtime.start_heartbeat(interval_seconds=0.01, ttl_seconds=10)
        await _wait_for_write_calls(store, at_least=2)
        assert runtime._heartbeat_task is not None  # noqa: SLF001
        assert not runtime._heartbeat_task.done()  # noqa: SLF001
        live = await store.read_all()
        assert "runtime-hb-1" in live, "later heartbeat never actually succeeded"
        await runtime.shutdown()

    asyncio.run(scenario())


def test_heartbeat_repeated_failures_retry_at_a_bounded_rate_not_a_tight_loop(monkeypatch) -> None:
    """Test 2: several consecutive failures must not produce a tight,
    unbounded retry loop -- retries are paced by `interval_seconds`, and the
    task survives every one of them."""
    interval_seconds = 0.02
    failures = 8
    store = _ScriptedHeartbeatStore([TimeoutError("redis timeout")] * failures)
    runtime = CallRuntime(
        instance_id="runtime-hb-2",
        address="ws://runtime-hb-2/media",
        capacity=10,
        heartbeat_store=store,
    )

    async def scenario() -> float:
        started_at = time.monotonic()
        runtime.start_heartbeat(interval_seconds=interval_seconds, ttl_seconds=10)
        # One write past the scripted failures proves the loop kept retrying
        # all the way through them rather than dying partway.
        await _wait_for_write_calls(store, at_least=failures + 1)
        elapsed = time.monotonic() - started_at
        assert runtime._heartbeat_task is not None  # noqa: SLF001
        assert not runtime._heartbeat_task.done()  # noqa: SLF001
        await runtime.shutdown()
        return elapsed

    elapsed = asyncio.run(scenario())
    # A tight loop would clear `failures + 1` calls in microseconds; pacing
    # by `interval_seconds` means it must take at least most of that many
    # intervals (generous lower bound to absorb scheduler jitter) -- and an
    # equally generous upper bound rules out runaway/unbounded backoff.
    assert elapsed >= interval_seconds * failures * 0.5, elapsed
    assert elapsed < interval_seconds * failures * 10, elapsed


def test_heartbeat_resumes_after_a_failure_recovery_failure_recovery_sequence(monkeypatch) -> None:
    """Test 3: success / failure / failure / success / success -- heartbeat
    emission must resume automatically each time, not just once."""
    store = _ScriptedHeartbeatStore([None, RuntimeError("blip"), RuntimeError("blip"), None, None])
    runtime = CallRuntime(
        instance_id="runtime-hb-3",
        address="ws://runtime-hb-3/media",
        capacity=10,
        heartbeat_store=store,
    )

    async def scenario() -> None:
        runtime.start_heartbeat(interval_seconds=0.01, ttl_seconds=10)
        await _wait_for_write_calls(store, at_least=5)
        # Give the loop one more tick past the scripted sequence so the
        # final (unscripted, always-succeeding) write actually lands.
        await _wait_for_write_calls(store, at_least=6)
        live = await store.read_all()
        assert "runtime-hb-3" in live, "heartbeat did not resume after recovery"
        assert runtime._heartbeat_task is not None  # noqa: SLF001
        assert not runtime._heartbeat_task.done()  # noqa: SLF001
        await runtime.shutdown()

    asyncio.run(scenario())


def test_heartbeat_cancellation_still_propagates_while_retrying(monkeypatch) -> None:
    """Test 4: shutdown() while the loop is mid-outage (about to sleep/retry)
    must still cancel the heartbeat task cleanly and promptly -- a Redis
    failure must never be mistaken for a reason to keep retrying forever,
    ignoring cancellation."""
    store = _ScriptedHeartbeatStore([ConnectionError("down")] * 1000)  # never recovers
    runtime = CallRuntime(
        instance_id="runtime-hb-4",
        address="ws://runtime-hb-4/media",
        capacity=10,
        heartbeat_store=store,
    )

    async def scenario() -> None:
        runtime.start_heartbeat(interval_seconds=0.01, ttl_seconds=10)
        await _wait_for_write_calls(store, at_least=3)
        await asyncio.wait_for(runtime.shutdown(), timeout=2)
        assert runtime._heartbeat_task is None  # noqa: SLF001

    asyncio.run(scenario())  # must not hang, must not raise


def test_heartbeat_failure_does_not_alter_call_ownership_or_cancel_active_calls(
    monkeypatch,
) -> None:
    """Tests 5 and 6: a Redis heartbeat failure must not touch this
    runtime's own ownership bookkeeping (`_tasks`/`_cancellations`) or cause
    an active call's own cancellation -- heartbeat is a liveness signal only
    (`voiceagent.runtime.heartbeat` module docstring), never the authority
    `run_call_task()`'s own cancellation is driven by. Reuses the existing
    ownership/call-task primitives already exercised elsewhere in this file
    rather than inventing a new ownership model or integration framework."""
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def fake_run_call_task(*, context, call_session_id, call_ref, deps, cancellation):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(supervisor_module, "run_call_task", fake_run_call_task)
    store = _ScriptedHeartbeatStore([ConnectionError("down")] * 20)
    runtime = CallRuntime(
        instance_id="runtime-hb-5",
        address="ws://runtime-hb-5/media",
        capacity=10,
        heartbeat_store=store,
    )
    call_session_id = uuid.uuid4()

    async def scenario() -> None:
        await runtime.start_call(_context(), call_session_id, "ref-1", _fake_deps())
        await asyncio.wait_for(started.wait(), timeout=2)
        runtime.start_heartbeat(interval_seconds=0.01, ttl_seconds=10)
        await _wait_for_write_calls(store, at_least=10)  # several heartbeat failures land
        # Ownership/call state is completely untouched by any of them.
        assert runtime.is_running(call_session_id) is True
        assert runtime.current_load == 1
        assert call_session_id in runtime._tasks  # noqa: SLF001
        # Present since `start_call()` (default reason), untouched by any
        # heartbeat failure -- a real cancel would overwrite this reason.
        assert runtime._cancellations[call_session_id].reason == "hangup"  # noqa: SLF001
        assert cancelled.is_set() is False
        await runtime.shutdown()

    asyncio.run(scenario())
    assert cancelled.is_set() is True  # only shutdown()'s own cancellation touched it
