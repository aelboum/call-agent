"""`CallAiAnalysisWorker`'s Phase 2.14 metrics recording: one
`voiceagent.metrics.record_worker_tick()` call per `poll_once()`, tagged
`worker="call_intelligence"`."""

from __future__ import annotations

import asyncio
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


@pytest.fixture
def recorded_ticks(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(
        worker_module,
        "record_worker_tick",
        lambda worker, *, claimed, failed, tenants_errored, duration_seconds: calls.append(
            {
                "worker": worker,
                "claimed": claimed,
                "failed": failed,
                "tenants_errored": tenants_errored,
            }
        ),
    )
    return calls


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


def test_successful_tick_records_claimed_count(db, monkeypatch, recorded_ticks) -> None:
    tenant_id = uuid.uuid4()

    async def _fake_run(**kwargs):
        return object()

    monkeypatch.setattr(worker_module, "run_call_ai_analysis", _fake_run)

    worker = _worker(db, tenant_ids=lambda: [tenant_id], max_claims_per_tenant_per_tick=1)
    asyncio.run(worker.poll_once())

    assert recorded_ticks == [
        {"worker": "call_intelligence", "claimed": 1, "failed": 0, "tenants_errored": 0}
    ]


def test_tenant_failure_records_tenants_errored(db, monkeypatch, recorded_ticks) -> None:
    tenant_id = uuid.uuid4()

    async def _fake_run(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker_module, "run_call_ai_analysis", _fake_run)

    worker = _worker(db, tenant_ids=lambda: [tenant_id])
    asyncio.run(worker.poll_once())

    assert recorded_ticks == [
        {"worker": "call_intelligence", "claimed": 0, "failed": 0, "tenants_errored": 1}
    ]
