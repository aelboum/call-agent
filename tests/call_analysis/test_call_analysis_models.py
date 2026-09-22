"""`CallAnalysis` schema shape (SQLAlchemy metadata introspection, no
database needed)."""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from voiceagent.call_analysis.models import ANALYSIS_STATUSES, CallAnalysis


def test_call_analysis_table_shape() -> None:
    table = cast(Table, CallAnalysis.__table__)
    assert table.schema == "app"
    assert table.name == "call_analysis"
    assert {c.name for c in table.columns} == {
        "id",
        "tenant_id",
        "call_session_id",
        "status",
        "turn_count",
        "user_turn_count",
        "assistant_turn_count",
        "tool_call_count",
        "tool_result_count",
        "duration_ms",
        "had_transfer",
        "had_hold",
        "contact_associated",
        "outcome",
        "follow_up_count",
        "appointment_follow_up_count",
        "open_follow_up_count",
        "created_at",
        "updated_at",
    }
    assert not table.columns["call_session_id"].nullable
    assert not table.columns["status"].nullable
    assert not table.columns["turn_count"].nullable
    assert table.columns["duration_ms"].nullable
    assert table.columns["outcome"].nullable


def test_call_analysis_call_session_is_unique() -> None:
    """Brief §3/§9: at most one analysis per call session."""
    table = cast(Table, CallAnalysis.__table__)
    unique_column_sets = [
        tuple(sorted(col.name for col in c.columns))
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    ]
    assert ("call_session_id",) in unique_column_sets


def test_call_analysis_status_check_lists_exactly_pending_and_ready() -> None:
    table = cast(Table, CallAnalysis.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    (status_check,) = [text for text in checks if "pending" in text]
    assert "ready" in status_check
    assert ANALYSIS_STATUSES == {"pending", "ready"}


def test_call_analysis_composite_fk_is_tenant_aware() -> None:
    table = cast(Table, CallAnalysis.__table__)
    composite = [fk for fk in table.foreign_key_constraints if len(fk.columns) == 2]
    assert len(composite) == 1
    (fk,) = composite
    local = {col.name for col in fk.columns}
    assert "tenant_id" in local
    assert next(iter(fk.elements)).column.table.name == "call_sessions"


def test_no_delete_or_write_back_to_authoritative_domains_exists() -> None:
    """Brief §4: `CallAnalysis` must never become a second source of truth
    -- there is no service function that deletes an analysis, and none that
    writes to `CallSession`/`ConversationTurn`/`CallOutcome`/
    `FollowUpAction`."""
    import inspect

    from voiceagent.call_analysis import service

    for name, _obj in inspect.getmembers(service, inspect.isfunction):
        assert "delete" not in name, name
