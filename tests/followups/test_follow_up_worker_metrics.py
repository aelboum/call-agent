"""`FollowUpWorker`'s Phase 2.14 metrics recording: one
`voiceagent.metrics.record_worker_tick()` call per `poll_once()`, tagged
`worker="followups"`, with `claimed`/`tenants_errored` matching the tick's
own `WorkerTickReport`."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest

from voiceagent.followups import worker as worker_module
from voiceagent.followups.worker import FollowUpWorker
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


def test_successful_tick_records_claimed_count(db, monkeypatch, recorded_ticks) -> None:
    tenant_id = uuid.uuid4()
    monkeypatch.setattr(worker_module, "execute_due_follow_up", lambda context: object())

    worker = FollowUpWorker(
        db=db,
        tenant_ids=lambda: [tenant_id],
        system_actor_user_id=uuid.uuid4(),
        max_claims_per_tenant_per_tick=1,
    )
    asyncio.run(worker.poll_once())

    assert recorded_ticks == [
        {"worker": "followups", "claimed": 1, "failed": 0, "tenants_errored": 0}
    ]


def test_tenant_failure_records_tenants_errored(db, monkeypatch, recorded_ticks) -> None:
    tenant_id = uuid.uuid4()

    def _raise(context):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker_module, "execute_due_follow_up", _raise)

    worker = FollowUpWorker(
        db=db, tenant_ids=lambda: [tenant_id], system_actor_user_id=uuid.uuid4()
    )
    asyncio.run(worker.poll_once())

    assert recorded_ticks == [
        {"worker": "followups", "claimed": 0, "failed": 0, "tenants_errored": 1}
    ]


def test_empty_tick_still_records_a_zero_tick(db, recorded_ticks) -> None:
    worker = FollowUpWorker(db=db, tenant_ids=lambda: [], system_actor_user_id=uuid.uuid4())
    asyncio.run(worker.poll_once())
    assert recorded_ticks == [
        {"worker": "followups", "claimed": 0, "failed": 0, "tenants_errored": 0}
    ]
