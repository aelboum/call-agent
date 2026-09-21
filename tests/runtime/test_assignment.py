"""`select_runtime_for_assignment()` (ADR-0008 points 5, 6, 14) -- pure
selection logic, no database. `assign_call_to_runtime()`'s own database
write is exercised in `tests/integration/test_runtime_integration.py`."""

from __future__ import annotations

from voiceagent.runtime.assignment import select_runtime_for_assignment
from voiceagent.runtime.heartbeat import RuntimeHeartbeat


def _heartbeat(instance_id: str, *, load: int, capacity: int = 10) -> RuntimeHeartbeat:
    return RuntimeHeartbeat(
        instance_id=instance_id,
        address=f"ws://{instance_id}/media",
        capacity=capacity,
        current_load=load,
        last_heartbeat_epoch_seconds=0.0,
    )


def test_selects_the_least_loaded_runtime_with_capacity() -> None:
    heartbeats = {
        "a": _heartbeat("a", load=5),
        "b": _heartbeat("b", load=1),
        "c": _heartbeat("c", load=3),
    }
    selected = select_runtime_for_assignment(heartbeats)
    assert selected is not None
    assert selected.instance_id == "b"


def test_a_runtime_at_its_ceiling_is_never_selected() -> None:
    heartbeats = {
        "full": _heartbeat("full", load=10, capacity=10),
        "available": _heartbeat("available", load=9, capacity=10),
    }
    selected = select_runtime_for_assignment(heartbeats)
    assert selected is not None
    assert selected.instance_id == "available"


def test_no_capacity_anywhere_returns_none() -> None:
    heartbeats = {
        "a": _heartbeat("a", load=10, capacity=10),
        "b": _heartbeat("b", load=5, capacity=5),
    }
    assert select_runtime_for_assignment(heartbeats) is None


def test_no_runtimes_at_all_returns_none() -> None:
    assert select_runtime_for_assignment({}) is None
