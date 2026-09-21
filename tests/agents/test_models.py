"""`Agent`/`AgentVersion` schema shape, verified against SQLAlchemy metadata
directly -- no database connection needed (`Table.columns`/`.constraints` are
pure Python introspection of the declared model). This pins the model against
`docs/PHASE-2.0-ARCHITECTURE.md` §23.2/§23.3's exact contract; the live-RLS/
trigger/constraint-*enforcement* behavior is `tests/integration/
test_domain_rls_integration.py` (real PostgreSQL, `-m integration`).
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Table, UniqueConstraint

from voiceagent.agents.models import Agent, AgentVersion


def _column_names(table: Table) -> set[str]:
    return {column.name for column in table.columns}


def _unique_constraints(table: Table) -> list[tuple[str, ...]]:
    return [
        tuple(col.name for col in c.columns)
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    ]


def _fk_constraints(table: Table) -> list[ForeignKeyConstraint]:
    return list(table.foreign_key_constraints)


def test_agents_table_shape() -> None:
    table = cast(Table, Agent.__table__)
    assert table.schema == "app"
    assert table.name == "agents"
    assert _column_names(table) == {
        "id",
        "tenant_id",
        "name",
        "description",
        "status",
        "draft_version_id",
        "published_version_id",
        "created_at",
        "updated_at",
    }
    assert not table.columns["tenant_id"].nullable
    assert not table.columns["name"].nullable
    assert table.columns["description"].nullable
    assert table.columns["draft_version_id"].nullable
    assert ("tenant_id", "name") in _unique_constraints(table)
    assert ("id", "tenant_id") in _unique_constraints(table)


def test_agents_status_check_constraint() -> None:
    table = cast(Table, Agent.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    assert any("active" in text and "archived" in text for text in checks)


def test_agents_composite_fks_into_agent_versions() -> None:
    table = cast(Table, Agent.__table__)
    fks = _fk_constraints(table)
    composite = [fk for fk in fks if len(fk.columns) == 2]
    assert len(composite) == 2
    for fk in composite:
        local = {col.name for col in fk.columns}
        assert local in ({"draft_version_id", "tenant_id"}, {"published_version_id", "tenant_id"})
        referred = {element.column.name for element in fk.elements}
        assert referred == {"id", "tenant_id"}


def test_agent_versions_table_shape() -> None:
    table = cast(Table, AgentVersion.__table__)
    assert table.schema == "app"
    assert table.name == "agent_versions"
    assert _column_names(table) == {
        "id",
        "tenant_id",
        "agent_id",
        "version_number",
        "status",
        "config",
        "config_hash",
        "published_at",
        "published_by",
        "created_at",
        "updated_at",
    }
    assert ("agent_id", "version_number") in _unique_constraints(table)
    assert ("id", "tenant_id") in _unique_constraints(table)


def test_agent_versions_published_by_has_no_foreign_key() -> None:
    """Phase 2.0 report §11.5 / Phase 2.1 brief §20: `published_by` must
    remain a non-FK identity reference -- SaaS-OS owns `core.users` identity
    erasure, and a product FK could block or complicate it."""
    table = cast(Table, AgentVersion.__table__)
    referencing_columns = {col.name for fk in table.foreign_key_constraints for col in fk.columns}
    assert "published_by" not in referencing_columns
    # And explicitly not pointed at core.users anywhere in the table.
    for fk in table.foreign_key_constraints:
        for element in fk.elements:
            assert "core.users" not in str(element.target_fullname)


def test_agent_versions_status_check_constraint() -> None:
    table = cast(Table, AgentVersion.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    assert any("draft" in text and "published" in text and "archived" in text for text in checks)


def test_agent_versions_config_hash_format_check_exists() -> None:
    table = cast(Table, AgentVersion.__table__)
    checks = [c.sqltext.text for c in table.constraints if isinstance(c, CheckConstraint)]
    assert any("config_hash" in text for text in checks)


def test_agent_versions_composite_fk_into_agents() -> None:
    table = cast(Table, AgentVersion.__table__)
    composite = [fk for fk in table.foreign_key_constraints if len(fk.columns) == 2]
    assert len(composite) == 1
    fk = composite[0]
    assert {col.name for col in fk.columns} == {"agent_id", "tenant_id"}
    assert {element.column.name for element in fk.elements} == {"id", "tenant_id"}
