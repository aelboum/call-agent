"""`Calendar`/`CalendarEvent` schema shape (SQLAlchemy metadata
introspection, no database needed)."""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from voiceagent.calendars.models import Calendar, CalendarEvent


def test_calendars_table_shape() -> None:
    table = cast(Table, Calendar.__table__)
    assert table.schema == "app"
    assert table.name == "calendars"
    assert {c.name for c in table.columns} == {
        "id",
        "tenant_id",
        "name",
        "timezone",
        "is_active",
        "created_at",
        "updated_at",
    }
    assert not table.columns["timezone"].nullable


def test_calendars_id_tenant_is_unique_for_composite_fk_targets() -> None:
    table = cast(Table, Calendar.__table__)
    unique_column_sets = [
        tuple(sorted(col.name for col in c.columns))
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    ]
    assert ("id", "tenant_id") in unique_column_sets


def test_calendar_events_table_shape() -> None:
    table = cast(Table, CalendarEvent.__table__)
    assert table.schema == "app"
    assert table.name == "calendar_events"
    assert {c.name for c in table.columns} == {
        "id",
        "tenant_id",
        "calendar_id",
        "contact_id",
        "title",
        "start_at",
        "end_at",
        "status",
        "created_at",
        "updated_at",
    }
    assert not table.columns["calendar_id"].nullable
    assert table.columns["contact_id"].nullable
    assert not table.columns["start_at"].nullable
    assert not table.columns["end_at"].nullable


def test_calendar_events_interval_check_exists() -> None:
    table = cast(Table, CalendarEvent.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    assert any("start_at" in text and "end_at" in text for text in checks)


def test_calendar_events_status_check_lists_exactly_scheduled_and_cancelled() -> None:
    table = cast(Table, CalendarEvent.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    (status_check,) = [text for text in checks if "scheduled" in text]
    assert "cancelled" in status_check


def test_calendar_events_composite_fks_are_tenant_aware() -> None:
    table = cast(Table, CalendarEvent.__table__)
    composite = [fk for fk in table.foreign_key_constraints if len(fk.columns) == 2]
    referenced_tables = set()
    for fk in composite:
        local = {col.name for col in fk.columns}
        assert "tenant_id" in local
        referenced_tables.add(next(iter(fk.elements)).column.table.name)
    assert referenced_tables == {"calendars", "contacts"}


def test_no_hard_delete_api_exists() -> None:
    """Brief §5/§7: cancellation is a state transition, never a row
    deletion -- there is no service function or route that deletes a
    `CalendarEvent`."""
    import inspect

    from voiceagent.calendars import service

    assert "delete_event" not in dir(service)
    for name, _obj in inspect.getmembers(service, inspect.isfunction):
        assert "delete" not in name, name
