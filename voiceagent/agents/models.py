"""`app.agents` / `app.agent_versions` (Phase 2.0 report §23.2, §23.3).

`Agent` is a mutable pointer; `AgentVersion` is the immutable, published
snapshot of everything that determines call behavior (ADR-0004). Nothing here
enforces the immutability guarantee -- that is a three-layer property
(ADR-0004: application, database trigger, runtime), and the database layer
(§23.6 of the Phase 2.0 report) lives in the migration, not in this module.

`draft_version_id`/`published_version_id` are declared as plain nullable
columns with their composite foreign keys added separately in
`__table_args__`, referencing `app.agent_versions` by table-name string. This
is not a Python circular-import problem (SQLAlchemy foreign keys are
string-targeted, resolved at mapper-configuration time, after every model
module is imported) -- the *migration* has the real ordering constraint
(Phase 2.0 report §23.1: `agents` is created before `agent_versions`, so the
two FK constraints on `agents` are added via `ALTER TABLE` afterward).

`agent_versions.published_by` is a plain `UUID` with **no** foreign key into
`core.users` -- a deliberate decision (Phase 2.0 report §11.5, restated in
Phase 2.1's own brief §20): SaaS-OS owns identity erasure for that table, and
a product-side FK could block or complicate an erasure the product does not
own the lifecycle of.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from voiceagent.db import (
    JSON,
    Base,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Mapped,
    String,
    Text,
    TimestampMixin,
    UniqueConstraint,
    UUIDPrimaryKeyMixin,
    mapped_column,
    tenant_table_args,
)

__all__ = ["Agent", "AgentVersion"]


class Agent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The stable, tenant-visible identity of an assistant. Carries no
    behavior itself -- `draft_version_id`/`published_version_id` point at the
    `AgentVersion` rows that do (Phase 2.0 report §6, Entity: Agent)."""

    __tablename__ = "agents"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    draft_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    published_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    __table_args__ = tenant_table_args(
        UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_name"),
        UniqueConstraint("id", "tenant_id", name="uq_agents_id_tenant"),
        CheckConstraint("status IN ('active', 'archived')", name="ck_agents_status"),
        ForeignKeyConstraint(
            ["draft_version_id", "tenant_id"],
            ["app.agent_versions.id", "app.agent_versions.tenant_id"],
            name="fk_agents_draft_version",
        ),
        ForeignKeyConstraint(
            ["published_version_id", "tenant_id"],
            ["app.agent_versions.id", "app.agent_versions.tenant_id"],
            name="fk_agents_published_version",
        ),
        Index("ix_agents_tenant_id", "tenant_id"),
    )


class AgentVersion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An immutable, published-or-draft snapshot of one `Agent`'s complete
    behavior (ADR-0004; Phase 2.0 report §9). `config` is validated against
    the documented shape (`voiceagent.agents.config`) at the application
    layer before this row is ever written -- the database only enforces the
    physical `JSON` type and the hash-format `CHECK`.

    Immutability of a `published` row is enforced in the migration
    (`app.forbid_published_agent_version_update()`, Phase 2.0 report §23.6),
    not here -- no SQLAlchemy event hook duplicates that guarantee, per
    Phase 2.1's own instruction not to rely on ORM-level enforcement alone.
    """

    __tablename__ = "agent_versions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft"
    )
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # No FK -- see module docstring and Phase 2.0 report S11.5.
    published_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    __table_args__ = tenant_table_args(
        ForeignKeyConstraint(
            ["agent_id", "tenant_id"],
            ["app.agents.id", "app.agents.tenant_id"],
            name="fk_agent_versions_agent",
        ),
        UniqueConstraint(
            "agent_id", "version_number", name="uq_agent_versions_agent_version_number"
        ),
        UniqueConstraint("id", "tenant_id", name="uq_agent_versions_id_tenant"),
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name="ck_agent_versions_status"
        ),
        CheckConstraint(
            "config_hash ~ '^[0-9a-f]{64}$'", name="ck_agent_versions_config_hash_format"
        ),
        CheckConstraint(
            "status <> 'published' OR published_at IS NOT NULL",
            name="ck_agent_versions_published_at_required",
        ),
        Index("ix_agent_versions_tenant_id", "tenant_id"),
        Index("ix_agent_versions_agent_status", "agent_id", "status"),
    )
