"""The persistence seam's exported surface (ADR-0007).

One test here matters more than the rest: `voiceagent.db` must never expose
`text` or `func`. `infra/db/orm.py` withholds them on the strength of two
live audits -- either one lets application code execute
`set_config('app.tenant_id', ...)` inside a tenant-scoped session, bypassing
Row-Level Security for the rest of the transaction and, with `is_local=false`,
poisoning the pooled connection past COMMIT.
"""

from __future__ import annotations

import voiceagent.db as db


def test_arbitrary_sql_primitives_are_not_reachable() -> None:
    """Not exported, not importable, not present as an attribute -- by any
    of the three routes someone would actually try."""
    for name in ("text", "func"):
        assert name not in db.__all__
        assert not hasattr(db, name)


def test_the_seam_exports_what_the_product_actually_needs() -> None:
    """A seam, not a second ORM: the declarative base, the mixins, the column
    types SaaS-OS sanctions, the two narrowed SQL functions, and the session
    scopes. Nothing else."""
    expected = {
        "Base",
        "UUIDPrimaryKeyMixin",
        "TimestampMixin",
        "Mapped",
        "mapped_column",
        "String",
        "Text",
        "Integer",
        "Numeric",
        "Boolean",
        "DateTime",
        "JSON",
        "ForeignKey",
        "UniqueConstraint",
        "CheckConstraint",
        "Index",
        "now",
        "sum_",
        "select",
        "update",
        "delete",
        "session_scope",
        "tenant_session_scope",
        "acquire_tenant_advisory_lock",
    }
    assert expected <= set(db.__all__)
    for name in db.__all__:
        assert hasattr(db, name), name


def test_no_second_orm_abstraction() -> None:
    """ADR-0007 decision point 7: no repository base class, no unit-of-work,
    no query builder, no session wrapper. The seam exists so an upstream
    change is a one-file fix, not to reinvent SQLAlchemy."""
    suspicious = {"Repository", "UnitOfWork", "QueryBuilder", "BaseRepository", "SessionManager"}
    assert suspicious.isdisjoint(set(dir(db)))


def test_tenant_table_args_pins_the_product_schema() -> None:
    args = db.tenant_table_args()
    assert args == ({"schema": "app"},)

    index = db.Index("ix_example_tenant_id", "tenant_id")
    assert db.tenant_table_args(index) == (index, {"schema": "app"})
