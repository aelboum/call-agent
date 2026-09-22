"""`Contact` schema shape (SQLAlchemy metadata introspection, no database
needed). See `tests/agents/test_models.py`'s module docstring for why this
level is hermetic and what the integration suite covers instead."""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from voiceagent.contacts.models import Contact


def test_contacts_table_shape() -> None:
    table = cast(Table, Contact.__table__)
    assert table.schema == "app"
    assert table.name == "contacts"
    assert {c.name for c in table.columns} == {
        "id",
        "tenant_id",
        "name",
        "phone_e164",
        "email",
        "created_at",
        "updated_at",
    }
    assert not table.columns["name"].nullable
    assert not table.columns["phone_e164"].nullable
    assert table.columns["email"].nullable


def test_phone_e164_is_tenant_locally_unique_not_globally_unique() -> None:
    """Deliberately the opposite of `PhoneNumber.e164` (Phase 2.1): a
    contact's phone number is not a platform routing resource, so two
    different tenants each having a contact who shares a number is ordinary
    data, never a tenant-isolation failure."""
    table = cast(Table, Contact.__table__)
    unique_column_sets = [
        tuple(sorted(col.name for col in c.columns))
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    ]
    assert ("phone_e164", "tenant_id") in unique_column_sets
    assert ("phone_e164",) not in unique_column_sets
    assert ("id", "tenant_id") in unique_column_sets


def test_phone_e164_format_check_exists() -> None:
    table = cast(Table, Contact.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    assert any("phone_e164" in text for text in checks)
