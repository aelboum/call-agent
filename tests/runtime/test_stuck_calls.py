"""`find_stuck_call_sessions()` (Phase 2.14 brief section 13) -- pure
filtering logic against plain `CallSession` instances, no database. Mirrors
`tests/runtime/test_reconciliation.py`'s own fixture technique exactly."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from voiceagent.calls.models import CallSession
from voiceagent.runtime.heartbeat import RuntimeHeartbeat
from voiceagent.runtime.stuck_calls import find_stuck_call_sessions

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _call(*, status: str, runtime_instance_id: str | None, age_seconds: float) -> CallSession:
    call = CallSession()
    call.id = uuid.uuid4()
    call.status = status
    call.runtime_instance_id = runtime_instance_id
    call.updated_at = _NOW - timedelta(seconds=age_seconds)
    return call


def _live_heartbeats(*instance_ids: str) -> dict[str, RuntimeHeartbeat]:
    return {
        instance_id: RuntimeHeartbeat(
            instance_id=instance_id,
            address="ws://x/media",
            capacity=10,
            current_load=0,
            last_heartbeat_epoch_seconds=0.0,
        )
        for instance_id in instance_ids
    }


def _find(calls, heartbeats, *, startup=60.0, active=3600.0):
    return find_stuck_call_sessions(
        calls,
        heartbeats,
        now=_NOW,
        startup_threshold_seconds=startup,
        active_threshold_seconds=active,
    )


def test_a_call_stuck_in_startup_past_threshold_is_reported() -> None:
    call = _call(status="ringing", runtime_instance_id="runtime-1", age_seconds=120)
    result = _find([call], _live_heartbeats("runtime-1"), startup=60.0)
    assert len(result) == 1
    assert result[0].phase == "startup"
    assert result[0].call_session_id == call.id
    assert result[0].seconds_since_progress == 120


def test_a_call_stuck_active_past_threshold_is_reported() -> None:
    call = _call(status="in_progress", runtime_instance_id="runtime-1", age_seconds=4000)
    result = _find([call], _live_heartbeats("runtime-1"), active=3600.0)
    assert len(result) == 1
    assert result[0].phase == "active"


def test_a_call_within_threshold_is_not_reported() -> None:
    call = _call(status="ringing", runtime_instance_id="runtime-1", age_seconds=10)
    assert _find([call], _live_heartbeats("runtime-1"), startup=60.0) == []


def test_a_call_whose_runtime_heartbeat_has_expired_is_not_reported() -> None:
    """Reconciliation's job, not this one's -- see this module's own
    docstring: a crashed runtime's calls are `find_stale_call_sessions()`'s
    business."""
    call = _call(status="in_progress", runtime_instance_id="runtime-crashed", age_seconds=9999)
    assert _find([call], _live_heartbeats("runtime-1")) == []


def test_a_call_with_no_runtime_assigned_yet_is_never_stuck() -> None:
    call = _call(status="initiated", runtime_instance_id=None, age_seconds=9999)
    assert _find([call], {}) == []


def test_a_terminal_looking_status_outside_startup_and_active_is_skipped() -> None:
    """`non_terminal` callers only ever pass non-terminal statuses in
    practice, but this function does not assume that -- any status outside
    the two known phases is simply not this detector's concern."""
    call = _call(status="completed", runtime_instance_id="runtime-1", age_seconds=9999)
    assert _find([call], _live_heartbeats("runtime-1")) == []


def test_mixed_set_returns_only_the_stuck_ones() -> None:
    fine = _call(status="ringing", runtime_instance_id="runtime-1", age_seconds=5)
    stuck = _call(status="answered", runtime_instance_id="runtime-1", age_seconds=9999)
    crashed = _call(status="in_progress", runtime_instance_id="runtime-2", age_seconds=9999)
    result = _find([fine, stuck, crashed], _live_heartbeats("runtime-1"), active=3600.0)
    assert [r.call_session_id for r in result] == [stuck.id]
