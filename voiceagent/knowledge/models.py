"""`app.knowledge_sources` / `app.knowledge_items` (Phase 2.11).

Two tables, matching `voiceagent.followups.models`'s own two-tables-one-module
shape. `KnowledgeSource` is a plain tenant-owned grouping (a name/description
label, never itself retrieved); `KnowledgeItem` is the unit of retrieval --
short, textual, tenant-owned content, bounded in size (`MAX_CONTENT_LENGTH`)
so this never becomes a generic document store (brief NON-GOALS).

**Content immutability once out of `draft`** (see the package docstring for
why): migration `0009` installs `app.forbid_knowledge_item_content_update`,
a trigger mirroring `app.forbid_published_agent_version_update`
(migration `0002`) -- once a row's `status` leaves `'draft'`, `title` and
`content` can never change again, and the only further status transition the
trigger permits is `'active' -> 'archived'`. This module states that
invariant; it cannot enforce it itself (an ORM model grants no DDL, ADR-0007).
"""

from __future__ import annotations

import uuid

from voiceagent.db import (
    Base,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Mapped,
    String,
    Text,
    TimestampMixin,
    UniqueConstraint,
    UUIDPrimaryKeyMixin,
    mapped_column,
    tenant_table_args,
)

__all__ = [
    "KNOWLEDGE_ITEM_STATUSES",
    "KNOWLEDGE_SOURCE_STATUSES",
    "MAX_KNOWLEDGE_ITEM_CONTENT_LENGTH",
    "MAX_KNOWLEDGE_ITEM_TITLE_LENGTH",
    "KnowledgeItem",
    "KnowledgeSource",
]

KNOWLEDGE_SOURCE_STATUSES = frozenset({"active", "archived"})

#: `draft` (mutable, never retrievable), `active` (retrievable, content
#: frozen), `archived` (content frozen, excluded from retrieval -- the
#: "deactivated" state; brief RBAC calls the transition into it
#: "deactivate").
KNOWLEDGE_ITEM_STATUSES = frozenset({"draft", "active", "archived"})

MAX_KNOWLEDGE_ITEM_TITLE_LENGTH = 200
#: Deliberately small -- a business FAQ/policy/hours entry, not a document
#: (brief NON-GOALS: "not a generic document store"). Also bounds the worst
#: case a single `voiceagent.knowledge.retrieval.search_items()` result can
#: return to the model before this module's own snippet truncation even
#: applies.
MAX_KNOWLEDGE_ITEM_CONTENT_LENGTH = 20_000


class KnowledgeSource(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A tenant's own grouping of `KnowledgeItem` rows (e.g. "Business
    Hours", "Pricing", "FAQs") -- never itself retrieved or sent to an LLM;
    only its individual, individually-approved items are
    (`voiceagent.knowledge.config.KnowledgeConfig`)."""

    __tablename__ = "knowledge_sources"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )

    __table_args__ = tenant_table_args(
        UniqueConstraint("id", "tenant_id", name="uq_knowledge_sources_id_tenant"),
        UniqueConstraint("tenant_id", "name", name="uq_knowledge_sources_tenant_name"),
        CheckConstraint("status IN ('active', 'archived')", name="ck_knowledge_sources_status"),
        Index("ix_knowledge_sources_tenant_id", "tenant_id"),
        Index("ix_knowledge_sources_status", "status"),
    )


class KnowledgeItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One tenant's piece of textual knowledge. Created `draft`, edited freely
    while `draft`, then `activate`d -- from that point its `title`/`content`
    are frozen for the row's entire remaining lifetime (see module
    docstring)."""

    __tablename__ = "knowledge_items"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(String(MAX_KNOWLEDGE_ITEM_TITLE_LENGTH), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft"
    )

    __table_args__ = tenant_table_args(
        UniqueConstraint("id", "tenant_id", name="uq_knowledge_items_id_tenant"),
        ForeignKeyConstraint(
            ["source_id", "tenant_id"],
            ["app.knowledge_sources.id", "app.knowledge_sources.tenant_id"],
            name="fk_knowledge_items_source",
        ),
        UniqueConstraint(
            "tenant_id", "source_id", "title", name="uq_knowledge_items_tenant_source_title"
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'archived')", name="ck_knowledge_items_status"
        ),
        CheckConstraint(
            f"length(content) <= {MAX_KNOWLEDGE_ITEM_CONTENT_LENGTH}",
            name="ck_knowledge_items_content_length",
        ),
        Index("ix_knowledge_items_tenant_id", "tenant_id"),
        Index("ix_knowledge_items_source_id", "source_id"),
        Index("ix_knowledge_items_tenant_status", "tenant_id", "status"),
    )
