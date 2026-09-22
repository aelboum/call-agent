"""`voiceagent.call_intelligence.worker.CallAiAnalysisWorker` -- hermetic: a
real `DatabaseBoundary` (just a bounded thread pool, no database) plus a
fake `run_call_ai_analysis()` (mirrors `tests/followups/test_followup_worker
.py` exactly -- bounded concurrency, graceful shutdown, cancellation,
per-tenant error isolation, no unbounded task creation)."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from voiceagent.call_intelligence import worker as worker_module
from voiceagent.call_intelligence.worker import CallAiAnalysisWorker
from voiceagent.config.settings import AiProviderSettings
from voiceagent.providers.call_intelligence.fakes import FakeCallIntelligenceProvider
from voiceagent.runtime.db import DatabaseBoundary


@pytest.fixture
def db() -> Iterator[DatabaseBoundary]:
    boundary = DatabaseBoundary(max_workers=8)
    yield boundary
    boundary.close()


def _run(coro):
    return asyncio.run(coro)


def _worker(db, **overrides: Any) -> CallAiAnalysisWorker:
    kwargs: dict[str, Any] = dict(
        db=db,
        tenant_ids=lambda: [],
        provider=FakeCallIntelligenceProvider(),
        provider_name="fake",
        ai_provider_settings=AiProviderSettings(),
        system_actor_user_id=uuid.uuid4(),
    )
    kwargs.update(overrides)
    return CallAiAnalysisWorker(**kwargs)


def test_poll_once_stops_after_max_claims_per_tenant_per_tick(db, monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    call_count = {"n": 0}
    lock = threading.Lock()

    async def _fake_run(**kwargs):
        with lock:
            call_count["n"] += 1
            return object()  # never None: an unbounded worker would loop forever

    monkeypatch.setattr(worker_module, "run_call_ai_analysis", _fake_run)

    worker = _worker(db, tenant_ids=lambda: [tenant_id], max_claims_per_tenant_per_tick=3)
    report = _run(worker.poll_once())

    assert call_count["n"] == 3
    assert report.executed == 3
    assert report.tenants_scanned == 1
    assert report.errored == 0


def test_poll_once_stops_early_once_nothing_is_due(db, monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    remaining = {"n": 2}
    lock = threading.Lock()

    async def _fake_run(**kwargs):
        with lock:
            if remaining["n"] <= 0:
                return None
            remaining["n"] -= 1
            return object()

    monkeypatch.setattr(worker_module, "run_call_ai_analysis", _fake_run)

    worker = _worker(db, tenant_ids=lambda: [tenant_id], max_claims_per_tenant_per_tick=10)
    report = _run(worker.poll_once())

    assert report.executed == 2


def test_poll_once_isolates_one_tenants_error_from_another(db, monkeypatch) -> None:
    broken_tenant = uuid.uuid4()
    healthy_tenant = uuid.uuid4()

    async def _fake_run(*, context, **kwargs):
        if context.tenant_id == broken_tenant:
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr(worker_module, "run_call_ai_analysis", _fake_run)

    worker = _worker(db, tenant_ids=lambda: [broken_tenant, healthy_tenant])
    report = _run(worker.poll_once())

    assert report.tenants_scanned == 2
    assert report.errored == 1
    (failed_tenant_id, error_message) = report.per_tenant_errors[0]
    assert failed_tenant_id == broken_tenant
    assert "RuntimeError" in error_message


def test_poll_once_respects_max_concurrent_tenants(db, monkeypatch) -> None:
    tenant_ids = [uuid.uuid4() for _ in range(6)]
    active = {"count": 0, "peak": 0}
    lock = threading.Lock()

    async def _fake_run(**kwargs):
        with lock:
            active["count"] += 1
            active["peak"] = max(active["peak"], active["count"])
        await asyncio.sleep(0.05)
        with lock:
            active["count"] -= 1
        return None

    monkeypatch.setattr(worker_module, "run_call_ai_analysis", _fake_run)

    worker = _worker(db, tenant_ids=lambda: tenant_ids, max_concurrent_tenants=2)
    _run(worker.poll_once())

    assert active["peak"] <= 2


def test_start_is_idempotent(db, monkeypatch) -> None:
    async def _fake_run(**kwargs):
        return None

    monkeypatch.setattr(worker_module, "run_call_ai_analysis", _fake_run)

    worker = _worker(db, poll_interval_seconds=60)

    async def _scenario():
        worker.start()
        first_task = worker._task
        worker.start()
        second_task = worker._task
        assert first_task is second_task
        await worker.shutdown()

    _run(_scenario())


def test_shutdown_cancels_the_running_loop(db, monkeypatch) -> None:
    tick_count = {"n": 0}

    async def _fake_run(**kwargs):
        tick_count["n"] += 1
        return None

    monkeypatch.setattr(worker_module, "run_call_ai_analysis", _fake_run)

    worker = _worker(db, tenant_ids=lambda: [uuid.uuid4()], poll_interval_seconds=0.01)

    async def _scenario():
        worker.start()
        await asyncio.sleep(0.05)
        await worker.shutdown()
        assert worker._task is None
        observed_after_shutdown = tick_count["n"]
        await asyncio.sleep(0.1)
        assert tick_count["n"] == observed_after_shutdown

    _run(_scenario())


def test_constructor_rejects_non_positive_tunables(db) -> None:
    with pytest.raises(ValueError):
        _worker(db, poll_interval_seconds=0)
    with pytest.raises(ValueError):
        _worker(db, max_claims_per_tenant_per_tick=0)
    with pytest.raises(ValueError):
        _worker(db, max_concurrent_tenants=0)
    with pytest.raises(ValueError):
        _worker(db, timeout_seconds=0)
