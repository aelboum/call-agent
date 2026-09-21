"""`find_stale_call_sessions()` (ADR-0008 point 11) -- pure filtering logic
against plain `CallSession` instances, no database. `reconcile_tenant()`'s
own reads/writes are exercised in
`tests/integration/test_runtime_integration.py`."""

from __future__ import annotations

import uuid

from voiceagent.calls.models import CallSession
from voiceagent.runtime.heartbeat import RuntimeHeartbeat
from voiceagent.runtime.reconciliation import find_stale_call_sessions


def _call(runtime_instance_id: str | None) -> CallSession:
    call = CallSession()
    call.id = uuid.uuid4()
    call.runtime_instance_id = runtime_instance_id
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


def test_a_call_owned_by_a_live_runtime_is_not_stale() -> None:
    call = _call("runtime-1")
    assert find_stale_call_sessions([call], _live_heartbeats("runtime-1")) == []


def test_a_call_owned_by_an_absent_runtime_is_stale() -> None:
    call = _call("runtime-crashed")
    assert find_stale_call_sessions([call], _live_heartbeats("runtime-1")) == [call]


def test_a_call_with_no_runtime_assigned_yet_is_never_stale() -> None:
    call = _call(None)
    assert find_stale_call_sessions([call], {}) == []


def test_mixed_set_returns_only_the_stale_ones() -> None:
    live = _call("runtime-1")
    stale = _call("runtime-2")
    unassigned = _call(None)
    result = find_stale_call_sessions([live, stale, unassigned], _live_heartbeats("runtime-1"))
    assert result == [stale]
