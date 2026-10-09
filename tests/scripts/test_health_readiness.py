"""Phase 2.40: proves the real `scripts/run_call_runtime.py`,
`scripts/run_call_intelligence_worker.py` and `scripts/run_followup_worker.py`
entrypoints each expose a process-level `/healthz`/`/readyz` HTTP contract
(`voiceagent.health`) that transitions correctly across their own real
startup and shutdown sequences -- closing the Phase 2.36 finding that only
`voiceagent.api.app` had an equivalent signal.

Self-contained (duplicates, rather than imports, the small script-loading/
event-loop helpers `tests/scripts/test_metrics_activation.py` already
established for Phase 2.39) so that file -- already reviewed and committed
-- is not touched by this phase.

Every test assigns its own `VOICEAGENT_HEALTH_PORT` so no two tests in this
file (or a parallel run) can collide on a listening socket, including the
"failed startup" tests, which deliberately never reach the script's own
`health_server.close()` call (the exception propagates instead, exactly
matching this product's existing fail-closed startup discipline) and so
leave their own socket bound for the rest of the process's life.
"""

from __future__ import annotations

import asyncio
import importlib.util
import signal as signal_module
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from core.config import Settings as PlatformSettings

from voiceagent.config import FreeSwitchSettings, Settings
from voiceagent.runtime.fakes import FakeHeartbeatStore

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _load_script(name: str) -> ModuleType:
    path = _SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_phase_2_40_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _settings() -> Settings:
    return Settings(platform=PlatformSettings(environment="test"))


def _run(coro) -> None:
    """A bare `loop.run_until_complete()` -- never `asyncio.run()`, whose
    own `Runner` (3.11+) installs a temporary `signal.signal(SIGINT, ...)`
    handler for the duration of the run (purely so Ctrl+C can interrupt
    `asyncio.run()` itself). `scripts/run_*.py` import the *real* `signal`
    module (there is exactly one per process), so a test that patches
    `module.signal.signal` to capture the script's own handlers would
    otherwise also capture the Runner's own registration first."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


async def _await_signal_handlers_registered(
    handlers: dict[int, Callable[..., None]], *, timeout_seconds: float = 2.0
) -> None:
    waited = 0.0
    step = 0.005
    while not handlers:
        await asyncio.sleep(step)
        waited += step
        if waited >= timeout_seconds:
            raise AssertionError("startup never reached signal registration")


async def _await_event(event: asyncio.Event, *, timeout_seconds: float = 2.0) -> None:
    await asyncio.wait_for(event.wait(), timeout=timeout_seconds)


async def _probe(port: int, path: str) -> bytes:
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=2.0)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode("ascii"))
        await writer.drain()
        return await asyncio.wait_for(reader.read(4096), timeout=2.0)
    finally:
        writer.close()


async def _probe_until(
    port: int, path: str, predicate: Callable[[bytes], bool], *, timeout_seconds: float = 2.0
) -> bytes:
    """Bounded retry loop -- the same idiom `tests/runtime/test_supervisor.py`'s
    own `_wait_for_write_calls()` (Phase 2.38) and this directory's own
    `_await_signal_handlers_registered()` (Phase 2.39) already use for
    waiting on an async state transition without a blind, arbitrary
    `sleep()`."""
    waited = 0.0
    step = 0.01
    last: bytes = b""
    while waited < timeout_seconds:
        try:
            last = await _probe(port, path)
        except (ConnectionError, OSError):
            last = b""
        if predicate(last):
            return last
        await asyncio.sleep(step)
        waited += step
    raise AssertionError(f"{path} never reached the expected state; last response: {last!r}")


class _AlwaysFailingHeartbeatStore(FakeHeartbeatStore):
    """Simulates Redis being permanently unreachable after this process has
    already started -- strictly harder than Phase 2.38's own transient-
    failure scripting, to prove liveness/readiness genuinely do not depend
    on Redis at all once this process is up, not merely that they survive a
    short blip."""

    async def write(self, heartbeat, *, ttl_seconds: float) -> None:  # noqa: ANN001
        raise ConnectionError("redis permanently unreachable (simulated)")


class _FakeFreeSwitchTransport:
    """Stands in for `scripts.run_call_runtime._FreeSwitchTransport`: a
    controllable, genuinely-suspending `start()` -- the one real
    `await` point this script's own startup sequence has before it can
    call `mark_ready()` -- without opening a real ESL/media socket."""

    def __init__(self, entered: asyncio.Event, resume: asyncio.Event) -> None:
        self._entered = entered
        self._resume = resume
        self.telephony = object()
        self.media = object()
        self.events = type("_Events", (), {"on_unrouted_offer": None})()

    async def start(self) -> None:
        self._entered.set()
        await self._resume.wait()

    async def stop(self) -> None:
        pass


def test_run_call_runtime_health_contract_full_lifecycle(monkeypatch) -> None:
    """Tests 1, 2, 4, 5, 8 (brief) for the call-runtime process.

    Uses a FreeSWITCH-configured settings object specifically because this
    script's own startup sequence has exactly one genuine suspension point
    before `mark_ready()` -- `await freeswitch_transport.start()` -- which
    this test holds open to observe the not-ready window; every other
    startup step here is synchronous (construction only, no I/O), so there
    is no other reliable place to catch this process mid-startup."""
    module = _load_script("run_call_runtime")
    port = 9210
    handlers: dict[int, Callable[..., None]] = {}
    entered_freeswitch_start = asyncio.Event()
    resume_freeswitch_start = asyncio.Event()

    def _settings_with_freeswitch() -> Settings:
        return Settings(
            platform=PlatformSettings(environment="test"),
            freeswitch=FreeSwitchSettings(esl_host="fake-esl-host"),
        )

    monkeypatch.setenv("VOICEAGENT_HEALTH_PORT", str(port))
    monkeypatch.setattr(module, "configure_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(module, "settings_from_env", _settings_with_freeswitch)
    monkeypatch.setattr(module, "validate_deployment_readiness", lambda settings: None)
    monkeypatch.setattr(
        module, "get_jobs_config", lambda: type("_Jobs", (), {"redis_url": "redis://unused/0"})()
    )
    monkeypatch.setattr(
        module, "RedisHeartbeatStore", lambda redis_url: _AlwaysFailingHeartbeatStore()
    )
    monkeypatch.setattr(
        module,
        "_FreeSwitchTransport",
        lambda settings: _FakeFreeSwitchTransport(
            entered_freeswitch_start, resume_freeswitch_start
        ),
    )
    monkeypatch.setattr(
        module.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler)
    )

    async def scenario() -> None:
        task = asyncio.create_task(module._run())
        try:
            # --- Test 1: not-ready before required startup completes ---
            # The health listener is already serving (it starts before
            # `_FreeSwitchTransport` is even constructed) but this process
            # is paused mid-startup, inside `freeswitch_transport.start()`.
            await _await_event(entered_freeswitch_start)
            assert b"200 OK" in await _probe(port, "/healthz")
            assert b"503" in await _probe(port, "/readyz")

            # --- Test 2: successful startup becomes ready ---
            resume_freeswitch_start.set()
            await _await_signal_handlers_registered(handlers)
            assert b"200 OK" in await _probe(port, "/healthz")
            assert b"200 OK" in await _probe(port, "/readyz")

            # --- Test 5 + 8: liveness/readiness independent of Redis,
            # health remains available while the runtime keeps running ---
            # The heartbeat loop keeps retrying against a store whose
            # `write()` always raises (Phase 2.38 semantics); liveness and
            # readiness must be completely unaffected -- this process's
            # own readiness contract never pings Redis.
            await asyncio.sleep(0.05)
            assert b"200 OK" in await _probe(port, "/healthz")
            assert b"200 OK" in await _probe(port, "/readyz")

            # --- Test 4: shutdown removes readiness ---
            handlers[signal_module.SIGTERM](signal_module.SIGTERM, None)
            await _probe_until(port, "/readyz", lambda r: b"503" in r)

            await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()

        # Listener terminated cleanly -- a new connection is refused, not
        # merely slow or still answering stale state.
        try:
            await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=1.0)
        except (TimeoutError, ConnectionError, OSError):
            pass
        else:
            raise AssertionError("health listener still accepting connections after shutdown")

    _run(scenario())


def test_run_call_runtime_failed_startup_never_becomes_ready(monkeypatch) -> None:
    """Test 3 (brief) for the call-runtime process: a required startup
    dependency (here, Redis/jobs configuration) failing must leave
    readiness false forever, and the process must fail according to its
    own existing (unguarded, fail-closed) startup semantics rather than
    falsely advertising readiness."""
    module = _load_script("run_call_runtime")
    port = 9211

    monkeypatch.setenv("VOICEAGENT_HEALTH_PORT", str(port))
    monkeypatch.setattr(module, "configure_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(module, "settings_from_env", _settings)
    monkeypatch.setattr(module, "validate_deployment_readiness", lambda settings: None)

    def _boom() -> object:
        raise RuntimeError("simulated jobs configuration failure")

    monkeypatch.setattr(module, "get_jobs_config", _boom)

    async def scenario() -> None:
        task = asyncio.create_task(module._run())
        # The health listener starts before `get_jobs_config()` is ever
        # called, so it is already up even though startup is about to fail.
        assert b"200 OK" in await _probe_until(port, "/healthz", lambda r: b"200 OK" in r)
        assert b"503" in await _probe(port, "/readyz")

        raised: BaseException | None = None
        try:
            await asyncio.wait_for(task, timeout=2)
        except RuntimeError as exc:
            raised = exc
        assert raised is not None and "simulated jobs configuration failure" in str(raised)

        # Readiness never became true at any point before the failure.
        assert b"503" in await _probe(port, "/readyz")

    _run(scenario())


def test_run_call_intelligence_worker_health_contract(monkeypatch) -> None:
    """Tests 1, 2, 4, 6, 7, 8 (brief) for the call-intelligence worker."""
    module = _load_script("run_call_intelligence_worker")
    port = 9212
    handlers: dict[int, Callable[..., None]] = {}
    drain_calls: list[str] = []

    async def _fake_drain_idle(self, tenant_id):  # noqa: ANN001
        drain_calls.append("idle")
        return (0, None)

    monkeypatch.setenv("VOICEAGENT_HEALTH_PORT", str(port))
    monkeypatch.setenv(
        "VOICEAGENT_CALL_INTELLIGENCE_WORKER_TENANT_IDS", "11111111-1111-1111-1111-111111111111"
    )
    monkeypatch.setattr(module, "configure_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(module, "settings_from_env", _settings)
    monkeypatch.setattr(module, "validate_deployment_readiness", lambda settings: None)
    monkeypatch.setattr(module.CallAiAnalysisWorker, "_drain_one_tenant", _fake_drain_idle)
    monkeypatch.setattr(
        module.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler)
    )

    async def scenario() -> None:
        task = asyncio.create_task(module._run())
        try:
            await _await_signal_handlers_registered(handlers)
            # --- Test 2 + 8 ---
            assert b"200 OK" in await _probe(port, "/healthz")
            assert b"200 OK" in await _probe(port, "/readyz")

            # --- Test 6: idle (zero jobs claimed, zero errors) stays ready ---
            await _probe_until(port, "/readyz", lambda r: b"200 OK" in r)
            assert drain_calls, "a tick must have actually run during this window"
            assert b"200 OK" in await _probe(port, "/readyz")

            # --- Test 4 ---
            handlers[signal_module.SIGTERM](signal_module.SIGTERM, None)
            await _probe_until(port, "/readyz", lambda r: b"503" in r)
            await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()

    _run(scenario())


def test_run_call_intelligence_worker_one_failed_tick_does_not_remove_readiness(
    monkeypatch,
) -> None:
    """Test 7 (brief): a normal, isolated per-tenant tick failure
    (`CallAiAnalysisWorker`'s own existing per-tenant failure isolation --
    `_drain_one_tenant()` returning an error for one tenant never raises
    out of `poll_once()`) must not make the process unready."""
    module = _load_script("run_call_intelligence_worker")
    port = 9213
    handlers: dict[int, Callable[..., None]] = {}
    drain_calls: list[str] = []

    async def _fake_drain_failing(self, tenant_id):  # noqa: ANN001
        drain_calls.append("failed")
        return (0, "simulated_tenant_failure")

    monkeypatch.setenv("VOICEAGENT_HEALTH_PORT", str(port))
    monkeypatch.setenv(
        "VOICEAGENT_CALL_INTELLIGENCE_WORKER_TENANT_IDS", "22222222-2222-2222-2222-222222222222"
    )
    monkeypatch.setattr(module, "configure_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(module, "settings_from_env", _settings)
    monkeypatch.setattr(module, "validate_deployment_readiness", lambda settings: None)
    monkeypatch.setattr(module.CallAiAnalysisWorker, "_drain_one_tenant", _fake_drain_failing)
    monkeypatch.setattr(
        module.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler)
    )

    async def scenario() -> None:
        task = asyncio.create_task(module._run())
        try:
            await _await_signal_handlers_registered(handlers)
            assert b"200 OK" in await _probe(port, "/readyz")

            # Let at least one failing tick land.
            while not drain_calls:
                await asyncio.sleep(0.005)
            assert b"200 OK" in await _probe(port, "/readyz"), (
                "a recoverable per-tenant tick failure must not remove readiness"
            )

            handlers[signal_module.SIGTERM](signal_module.SIGTERM, None)
            await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()

    _run(scenario())


def test_run_call_intelligence_worker_failed_startup_never_becomes_ready(monkeypatch) -> None:
    """Test 3 (brief) for the call-intelligence worker: the provider
    construction step failing (a genuinely required dependency -- no
    worker can run without one) must leave readiness false, and the
    process must fail rather than falsely advertise readiness."""
    module = _load_script("run_call_intelligence_worker")
    port = 9214

    monkeypatch.setenv("VOICEAGENT_HEALTH_PORT", str(port))
    monkeypatch.setenv(
        "VOICEAGENT_CALL_INTELLIGENCE_WORKER_TENANT_IDS", "33333333-3333-3333-3333-333333333333"
    )
    monkeypatch.setattr(module, "configure_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(module, "settings_from_env", _settings)
    monkeypatch.setattr(module, "validate_deployment_readiness", lambda settings: None)

    def _boom(provider, model, config):  # noqa: ANN001
        raise RuntimeError("simulated provider construction failure")

    monkeypatch.setattr(module, "create_call_intelligence_provider", _boom)

    async def scenario() -> None:
        task = asyncio.create_task(module._run())
        assert b"200 OK" in await _probe_until(port, "/healthz", lambda r: b"200 OK" in r)
        assert b"503" in await _probe(port, "/readyz")

        raised: BaseException | None = None
        try:
            await asyncio.wait_for(task, timeout=2)
        except RuntimeError as exc:
            raised = exc
        assert raised is not None and "simulated provider construction failure" in str(raised)
        assert b"503" in await _probe(port, "/readyz")

    _run(scenario())


def test_run_followup_worker_health_contract(monkeypatch) -> None:
    """Tests 1, 2, 4, 6, 8 (brief) for the follow-up worker."""
    module = _load_script("run_followup_worker")
    port = 9215
    handlers: dict[int, Callable[..., None]] = {}
    drain_calls: list[str] = []

    async def _fake_drain_idle(self, tenant_id):  # noqa: ANN001
        drain_calls.append("idle")
        return (0, None)

    monkeypatch.setenv("VOICEAGENT_HEALTH_PORT", str(port))
    monkeypatch.setenv(
        "VOICEAGENT_FOLLOWUP_WORKER_TENANT_IDS", "44444444-4444-4444-4444-444444444444"
    )
    monkeypatch.setenv(
        "VOICEAGENT_FOLLOWUP_WORKER_SYSTEM_ACTOR_USER_ID", "55555555-5555-5555-5555-555555555555"
    )
    monkeypatch.setattr(module, "configure_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(module, "settings_from_env", _settings)
    monkeypatch.setattr(module, "validate_deployment_readiness", lambda settings: None)
    monkeypatch.setattr(module.FollowUpWorker, "_drain_one_tenant", _fake_drain_idle)
    monkeypatch.setattr(
        module.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler)
    )

    async def scenario() -> None:
        task = asyncio.create_task(module._run())
        try:
            # --- Test 1 (checked before signal registration, i.e. before
            # `worker.start()`/`mark_ready()` -- the listener itself starts
            # immediately, well before tenant-ID parsing) ---
            assert b"200 OK" in await _probe_until(port, "/healthz", lambda r: b"200 OK" in r)

            # --- Test 2 + 8 ---
            await _await_signal_handlers_registered(handlers)
            assert b"200 OK" in await _probe(port, "/healthz")
            assert b"200 OK" in await _probe(port, "/readyz")

            # --- Test 6: idle stays ready ---
            while not drain_calls:
                await asyncio.sleep(0.005)
            assert b"200 OK" in await _probe(port, "/readyz")

            # --- Test 4 ---
            handlers[signal_module.SIGTERM](signal_module.SIGTERM, None)
            await _probe_until(port, "/readyz", lambda r: b"503" in r)
            await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()

    _run(scenario())


def test_run_followup_worker_failed_startup_never_becomes_ready(monkeypatch) -> None:
    """Test 3 (brief) for the follow-up worker: the required
    `VOICEAGENT_FOLLOWUP_WORKER_SYSTEM_ACTOR_USER_ID` dependency being
    unusable (here: `FollowUpWorker` construction itself failing) must
    leave readiness false."""
    module = _load_script("run_followup_worker")
    port = 9216

    monkeypatch.setenv("VOICEAGENT_HEALTH_PORT", str(port))
    monkeypatch.setenv(
        "VOICEAGENT_FOLLOWUP_WORKER_TENANT_IDS", "66666666-6666-6666-6666-666666666666"
    )
    monkeypatch.setenv(
        "VOICEAGENT_FOLLOWUP_WORKER_SYSTEM_ACTOR_USER_ID", "77777777-7777-7777-7777-777777777777"
    )
    monkeypatch.setattr(module, "configure_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(module, "settings_from_env", _settings)
    monkeypatch.setattr(module, "validate_deployment_readiness", lambda settings: None)

    def _boom(**kwargs):  # noqa: ANN003
        raise RuntimeError("simulated worker construction failure")

    monkeypatch.setattr(module, "FollowUpWorker", _boom)

    async def scenario() -> None:
        task = asyncio.create_task(module._run())
        assert b"200 OK" in await _probe_until(port, "/healthz", lambda r: b"200 OK" in r)
        assert b"503" in await _probe(port, "/readyz")

        raised: BaseException | None = None
        try:
            await asyncio.wait_for(task, timeout=2)
        except RuntimeError as exc:
            raised = exc
        assert raised is not None and "simulated worker construction failure" in str(raised)
        assert b"503" in await _probe(port, "/readyz")

    _run(scenario())
