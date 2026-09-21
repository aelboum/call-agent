"""The product's persistence seam (ADR-0007).

Every module in `voiceagent` that touches persistence imports from here and
from nowhere else. This module re-exports the primitives SaaS-OS sanctions in
`infra.db`, and adds the few product-specific helpers the application needs.
It is a seam, not a second ORM: there is no repository base class, no
unit-of-work, no query builder, and no session wrapper. Its value is that a
change to the upstream surface on a future re-pin is a one-file fix.

**`sqlalchemy.text` and `sqlalchemy.func` are not re-exported, and never will
be.** `infra/db/orm.py` withholds them on the strength of two live audits:
either one lets ordinary application code execute
`set_config('app.tenant_id', ...)` inside a tenant-scoped session, bypassing
Row-Level Security for the rest of that transaction and -- with
`is_local=false` -- poisoning the pooled connection past `COMMIT`. Reaching
around this module to obtain them would restore that capability class inside a
product whose most sensitive data is call recordings and transcripts. Use
`now()` and `sum_()` (named, single-purpose, incapable of expressing any other
PostgreSQL function call), and pass plain strings where SQLAlchemy accepts
them (`server_default="false"`, `Index(..., postgresql_where="...")`).

Physical column types are owned by `migrations/`, which imports SQLAlchemy
directly and may use any PostgreSQL type. A model's ORM type only has to be
compatible for bind/result handling: DDL is never generated from the models,
`create_all()` is never called, and Alembic autogenerate is disabled because
`Base.metadata` is shared with SaaS-OS's own tables.
"""

from __future__ import annotations

from infra.db import (
    JSON,
    Base,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    IntegrityError,
    Mapped,
    Numeric,
    OperationalError,
    Session,
    String,
    Text,
    TimestampMixin,
    UniqueConstraint,
    UUIDPrimaryKeyMixin,
    acquire_tenant_advisory_lock,
    delete,
    mapped_column,
    now,
    select,
    session_scope,
    sum_,
    tenant_session_scope,
    update,
)

__all__ = [
    "JSON",
    "PRODUCT_SCHEMA",
    "Base",
    "Boolean",
    "CheckConstraint",
    "DateTime",
    "ForeignKey",
    "ForeignKeyConstraint",
    "Index",
    "IntegrityError",
    "Integer",
    "Mapped",
    "Numeric",
    "OperationalError",
    "Session",
    "String",
    "Text",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "UniqueConstraint",
    "acquire_tenant_advisory_lock",
    "delete",
    "mapped_column",
    "now",
    "select",
    "session_scope",
    "sum_",
    "tenant_session_scope",
    "tenant_table_args",
    "update",
]

#: The product's PostgreSQL schema (ADR-0005). Frozen: it carries no product
#: or commercial identity precisely so that renaming the package or the
#: commercial product never implies a schema migration.
PRODUCT_SCHEMA = "app"


def tenant_table_args(*extra: object) -> tuple[object, ...]:
    """`__table_args__` for a product-owned, tenant-scoped table.

    Pins the table to the product schema and keeps that constant in one
    place. Row-Level Security itself is established by the migration via
    `infra.db.tenant_rls_statements()` -- a model can neither grant nor
    weaken it, which is the intended division (ADR-0007 decision point 6).
    """
    return (*extra, {"schema": PRODUCT_SCHEMA})
