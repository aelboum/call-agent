"""Phase 2.39: proves `voiceagent.metrics.configure_metrics()` is actually
invoked by the real production entrypoints that own every `record_*` call
site this product has (`voiceagent.metrics`'s own call sites: the
call-runtime's supervisor/orchestrator/reconciliation/stuck-call/media/
provider/tool-gateway modules, and both background workers' own
`record_worker_tick()` calls -- grepped, no other module calls `record_*`).

`voiceagent.api.app` is deliberately left out of this activation (and out of
this test file): grepping `voiceagent/api/` for any `voiceagent.metrics`
import or `record_*` call returns nothing, so initializing a `MeterProvider`
in that process would configure telemetry for a process that emits none of
it -- the brief's own "do not activate metrics blindly in every process".

Each script is loaded dynamically (`scripts/` is a collection of standalone
entrypoints, not a package -- no `scripts/__init__.py`, matching every other
file in that directory) rather than imported as `scripts.run_x`, so this
test adds no new package structure to the repository.

Every test replaces `configure_metrics` with a recording stub (never the
real OTel installer -- `tests/test_metrics.py`'s own module docstring
already documents that a real `MeterProvider` can only be installed once per
process, process-wide, so re-installing one here would corrupt that other
test file's own fixture if the two ever shared a test run) and every piece
of real I/O (`DATABASE_URL`/`REDIS_URL` point at an unreachable port in this
suite's own `tests/conftest.py`) with the repository's existing hermetic
fakes, then drives the script's real `_run()` coroutine through startup,
one tick, and a signal-triggered shutdown -- proving actual integration,
not `configure_metrics()` in isolation (already covered by
`tests/test_metrics.py`).
"""

from __future__ import annotations

import asyncio
import importlib.util
import signal as signal_module
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from core.config import Settings as PlatformSettings

from voiceagent.config import Settings
from voiceagent.runtime.fakes import FakeHeartbeatStore

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _load_script(name: str) -> ModuleType:
    """Load `scripts/<name>.py` as an importable module without turning
    `scripts/` into a package -- the identical technique used to unit-test
    any standalone script that is never `import`ed by production code."""
    path = _SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_phase_2_39_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _settings() -> Settings:
    # Defaults only: FreeSWITCH unconfigured (skips the real ESL/media
    # transport), `call_intelligence.provider == "fake"` (no vendor I/O).
    return Settings(platform=PlatformSettings(environment="test"))


class _RecordingConfigureMetrics:
    """Stands in for `voiceagent.metrics.configure_metrics` across a script's
    entire `_run()` -- records every call instead of touching OpenTelemetry's
    real (process-global, one-shot) `MeterProvider`."""

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.call_count += 1


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


def _run(coro) -> None:
    """`asyncio.run()`/`asyncio.Runner` (3.11+) installs its own temporary
    `signal.signal(SIGINT, ...)` handler for the duration of the run, purely
    so Ctrl+C can interrupt `asyncio.run()` itself -- and `scripts/run_*.py`
    import the *real* `signal` module (there is exactly one `signal` module
    object per process), so patching `module.signal.signal` patches that
    same global function, and the Runner's own startup registration would
    otherwise land in this test's `handlers` dict before the script under
    test ever runs a line of `_run()`. A bare `loop.run_until_complete()`
    installs no such handler."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def test_run_call_runtime_activates_metrics_once_before_startup_and_not_again_on_shutdown(
    monkeypatch,
) -> None:
    module = _load_script("run_call_runtime")
    recorder = _RecordingConfigureMetrics()
    handlers: dict[int, Callable[..., None]] = {}

    monkeypatch.setattr(module, "configure_metrics", recorder)
    monkeypatch.setattr(module, "settings_from_env", _settings)
    monkeypatch.setattr(module, "validate_deployment_readiness", lambda settings: None)
    monkeypatch.setattr(
        module, "get_jobs_config", lambda: type("_Jobs", (), {"redis_url": "redis://unused/0"})()
    )
    monkeypatch.setattr(module, "RedisHeartbeatStore", lambda redis_url: FakeHeartbeatStore())
    monkeypatch.setattr(
        module.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler)
    )

    async def scenario() -> None:
        task = asyncio.create_task(module._run())
        try:
            await _await_signal_handlers_registered(handlers)
            # Startup (heartbeat start, DB boundary, FreeSWITCH-skip branch)
            # is already past by the time signal handlers are registered --
            # configure_metrics() must have already run exactly once.
            assert recorder.call_count == 1

            handlers[signal_module.SIGTERM](signal_module.SIGTERM, None)
            await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()

        # Shutdown ran (CallRuntime.shutdown(), heartbeat_store.close(),
        # db.close()) without re-initializing metrics.
        assert recorder.call_count == 1

    _run(scenario())


def test_run_call_intelligence_worker_activates_metrics_once_before_first_tick(
    monkeypatch,
) -> None:
    module = _load_script("run_call_intelligence_worker")
    recorder = _RecordingConfigureMetrics()
    handlers: dict[int, Callable[..., None]] = {}

    monkeypatch.setenv(
        "VOICEAGENT_CALL_INTELLIGENCE_WORKER_TENANT_IDS", str(uuid.uuid4())
    )
    monkeypatch.setattr(module, "configure_metrics", recorder)
    monkeypatch.setattr(module, "settings_from_env", _settings)
    monkeypatch.setattr(module, "validate_deployment_readiness", lambda settings: None)
    monkeypatch.setattr(
        module.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler)
    )

    async def scenario() -> None:
        task = asyncio.create_task(module._run())
        try:
            await _await_signal_handlers_registered(handlers)
            assert recorder.call_count == 1

            # Let at least one real poll tick land (it records its own
            # `record_worker_tick()` regardless of whether the tenant's
            # own DB call succeeds -- per-tenant failure isolation,
            # `CallAiAnalysisWorker.poll_once()`'s own `_drain_one_tenant()`).
            await asyncio.sleep(0.05)
            assert recorder.call_count == 1, "a worker tick must never re-initialize metrics"

            handlers[signal_module.SIGTERM](signal_module.SIGTERM, None)
            await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()

        assert recorder.call_count == 1

    _run(scenario())


def test_run_followup_worker_activates_metrics_once_before_first_tick(monkeypatch) -> None:
    module = _load_script("run_followup_worker")
    recorder = _RecordingConfigureMetrics()
    handlers: dict[int, Callable[..., None]] = {}

    monkeypatch.setenv("VOICEAGENT_FOLLOWUP_WORKER_TENANT_IDS", str(uuid.uuid4()))
    monkeypatch.setenv("VOICEAGENT_FOLLOWUP_WORKER_SYSTEM_ACTOR_USER_ID", str(uuid.uuid4()))
    monkeypatch.setattr(module, "configure_metrics", recorder)
    monkeypatch.setattr(module, "settings_from_env", _settings)
    monkeypatch.setattr(module, "validate_deployment_readiness", lambda settings: None)
    monkeypatch.setattr(
        module.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler)
    )

    async def scenario() -> None:
        task = asyncio.create_task(module._run())
        try:
            await _await_signal_handlers_registered(handlers)
            assert recorder.call_count == 1

            await asyncio.sleep(0.05)
            assert recorder.call_count == 1, "a worker tick must never re-initialize metrics"

            handlers[signal_module.SIGTERM](signal_module.SIGTERM, None)
            await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()

        assert recorder.call_count == 1

    _run(scenario())



