"""`PhoneNumber` schema shape (SQLAlchemy metadata introspection, no
database needed). See `tests/agents/test_models.py`'s module docstring for
why this level is hermetic and what the integration suite covers instead.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from voiceagent.phone_numbers.models import PhoneNumber


def test_phone_numbers_table_shape() -> None:
    table = cast(Table, PhoneNumber.__table__)
    assert table.schema == "app"
    assert table.name == "phone_numbers"
    assert {c.name for c in table.columns} == {
        "id",
        "tenant_id",
        "e164",
        "label",
        "agent_id",
        "version_pin_mode",
        "pinned_version_id",
        "inbound_enabled",
        "outbound_caller_id",
        "ownership_verified_at",
        "created_at",
        "updated_at",
    }
    assert not table.columns["e164"].nullable
    assert table.columns["agent_id"].nullable


def test_e164_is_globally_unique_not_tenant_scoped() -> None:
    """The one security-critical assertion for this table (Phase 0 report
    §14.1 / Phase 2.0 report §15.1): the unique constraint is on `e164`
    ALONE. If this were ever changed to `(tenant_id, e164)`, two tenants
    could claim the identical DID -- an ambiguous-inbound-routing,
    tenant-isolation failure, not a cosmetic regression."""
    table = cast(Table, PhoneNumber.__table__)
    unique_column_sets = [
        tuple(sorted(col.name for col in c.columns))
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    ]
    assert ("e164",) in unique_column_sets
    assert ("e164", "tenant_id") not in unique_column_sets
    assert ("id", "tenant_id") in unique_column_sets


def test_pin_mode_consistency_check_exists() -> None:
    table = cast(Table, PhoneNumber.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    assert any("pinned_version_id" in text for text in checks)
    assert any("follow_published" in text and "pinned" in text for text in checks)


def test_composite_fks_are_tenant_aware() -> None:
    table = cast(Table, PhoneNumber.__table__)
    composite = [fk for fk in table.foreign_key_constraints if len(fk.columns) == 2]
    assert len(composite) == 2
    for fk in composite:
        local = {col.name for col in fk.columns}
        assert "tenant_id" in local
