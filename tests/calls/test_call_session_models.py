"""`CallSession` schema shape (SQLAlchemy metadata introspection, no
database needed)."""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Table

from voiceagent.calls.models import CallSession


def test_call_sessions_table_shape() -> None:
    table = cast(Table, CallSession.__table__)
    assert table.schema == "app"
    assert table.name == "call_sessions"
    assert {c.name for c in table.columns} == {
        "id",
        "tenant_id",
        "direction",
        "status",
        "from_e164",
        "to_e164",
        "phone_number_id",
        "agent_id",
        "agent_version_id",
        "fs_channel_uuid",
        "runtime_instance_id",
        "runtime_assigned_at",
        "started_at",
        "answered_at",
        "ended_at",
        "duration_ms",
        "hangup_cause",
        "end_reason",
        "data_authorization_decision_id",
        "created_at",
        "updated_at",
    }
    for required in ("tenant_id", "direction", "phone_number_id", "agent_id", "agent_version_id"):
        assert not table.columns[required].nullable, required


def test_no_runtime_assignments_table_exists() -> None:
    """ADR-0008 / Phase 2.1 brief §3, §13: runtime ownership is represented
    by columns on this table, never a separate table with its own
    reassignment history."""
    from voiceagent.db import Base

    assert "app.runtime_assignments" not in Base.metadata.tables


def test_data_authorization_decision_id_has_no_foreign_key() -> None:
    """Phase 2.0 report §23.5: this value correlates to a core.audit_log
    entry's own metadata -- it is not the primary key of any table."""
    table = cast(Table, CallSession.__table__)
    referencing = {col.name for fk in table.foreign_key_constraints for col in fk.columns}
    assert "data_authorization_decision_id" not in referencing


def test_status_check_constraint_lists_exactly_the_seven_states() -> None:
    table = cast(Table, CallSession.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    (status_check,) = [text for text in checks if "initiated" in text]
    for state in (
        "initiated",
        "ringing",
        "answered",
        "in_progress",
        "completed",
        "failed",
        "interrupted",
    ):
        assert state in status_check


def test_composite_fks_are_tenant_aware() -> None:
    table = cast(Table, CallSession.__table__)
    composite = [fk for fk in table.foreign_key_constraints if len(fk.columns) == 2]
    assert len(composite) == 3
    referenced_tables = set()
    for fk in composite:
        local = {col.name for col in fk.columns}
        assert "tenant_id" in local
        referenced_tables.add(next(iter(fk.elements)).column.table.name)
    assert referenced_tables == {"phone_numbers", "agents", "agent_versions"}
