"""`CallOutcome`/`FollowUpAction` schema shape (SQLAlchemy metadata
introspection, no database needed)."""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from voiceagent.followups.models import CallOutcome, FollowUpAction


def test_call_outcomes_table_shape() -> None:
    table = cast(Table, CallOutcome.__table__)
    assert table.schema == "app"
    assert table.name == "call_outcomes"
    assert {c.name for c in table.columns} == {
        "id",
        "tenant_id",
        "call_session_id",
        "contact_id",
        "outcome",
        "notes",
        "created_at",
        "updated_at",
    }
    assert not table.columns["call_session_id"].nullable
    assert not table.columns["outcome"].nullable
    assert table.columns["contact_id"].nullable
    assert table.columns["notes"].nullable


def test_call_outcomes_call_session_is_unique() -> None:
    """Brief §6: at most one current outcome per call session."""
    table = cast(Table, CallOutcome.__table__)
    unique_column_sets = [
        tuple(sorted(col.name for col in c.columns))
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    ]
    assert ("call_session_id",) in unique_column_sets


def test_call_outcomes_outcome_check_does_not_collide_with_call_session_status() -> None:
    """Brief §5: `CallOutcome.outcome` must not conflict with
    `CallSession.status`'s vocabulary -- verified directly, not just by
    docstring claim."""
    from voiceagent.calls.lifecycle import VALID_STATUSES
    from voiceagent.followups.models import OUTCOME_VALUES

    assert OUTCOME_VALUES.isdisjoint(VALID_STATUSES)


def test_call_outcomes_composite_fks_are_tenant_aware() -> None:
    table = cast(Table, CallOutcome.__table__)
    composite = [fk for fk in table.foreign_key_constraints if len(fk.columns) == 2]
    referenced_tables = set()
    for fk in composite:
        local = {col.name for col in fk.columns}
        assert "tenant_id" in local
        referenced_tables.add(next(iter(fk.elements)).column.table.name)
    assert referenced_tables == {"call_sessions", "contacts"}


def test_follow_up_actions_table_shape() -> None:
    table = cast(Table, FollowUpAction.__table__)
    assert table.schema == "app"
    assert table.name == "follow_up_actions"
    assert {c.name for c in table.columns} == {
        "id",
        "tenant_id",
        "call_session_id",
        "contact_id",
        "type",
        "status",
        "due_at",
        "calendar_event_id",
        "description",
        "created_at",
        "updated_at",
    }
    assert not table.columns["call_session_id"].nullable
    assert not table.columns["type"].nullable
    assert not table.columns["status"].nullable
    assert table.columns["due_at"].nullable
    assert table.columns["calendar_event_id"].nullable


def test_follow_up_actions_type_check_lists_exactly_three_values() -> None:
    table = cast(Table, FollowUpAction.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    (type_check,) = [text for text in checks if "appointment" in text and "IN" in text]
    for value in ("appointment", "contact", "manual_follow_up"):
        assert value in type_check


def test_follow_up_actions_status_check_lists_exactly_three_values() -> None:
    table = cast(Table, FollowUpAction.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    (status_check,) = [text for text in checks if "pending" in text]
    for value in ("pending", "completed", "cancelled"):
        assert value in status_check


def test_follow_up_actions_appointment_requires_calendar_event_check_exists() -> None:
    table = cast(Table, FollowUpAction.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    assert any("calendar_event_id" in text and "appointment" in text for text in checks)


def test_follow_up_actions_composite_fks_are_tenant_aware() -> None:
    table = cast(Table, FollowUpAction.__table__)
    composite = [fk for fk in table.foreign_key_constraints if len(fk.columns) == 2]
    referenced_tables = set()
    for fk in composite:
        local = {col.name for col in fk.columns}
        assert "tenant_id" in local
        referenced_tables.add(next(iter(fk.elements)).column.table.name)
    assert referenced_tables == {"call_sessions", "contacts", "calendar_events"}


def test_no_hard_delete_api_exists() -> None:
    """Brief §16: "do not expose DELETE for durable follow-up records" --
    there is no service function that deletes a `FollowUpAction` or
    `CallOutcome`."""
    import inspect

    from voiceagent.followups import service

    for name, _obj in inspect.getmembers(service, inspect.isfunction):
        assert "delete" not in name, name
