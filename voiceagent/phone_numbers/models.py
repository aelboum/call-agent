"""`app.phone_numbers` (Phase 2.0 report §23.4).

`e164` is **globally unique** -- deliberately not `(tenant_id, e164)` -- per
Phase 0 report §14.1: ambiguous inbound routing between two tenants claiming
the same DID is a tenant-isolation failure, not a UX annoyance. That single
column is the one place in this schema where a uniqueness violation is *not*
filtered by Row-Level Security (a `UNIQUE` constraint evaluates regardless of
what a query would return) -- see `voiceagent.phone_numbers.errors`/
`service` for the required generic-conflict handling this implies.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from voiceagent.db import (
    Base,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Mapped,
    String,
    TimestampMixin,
    UniqueConstraint,
    UUIDPrimaryKeyMixin,
    mapped_column,
    tenant_table_args,
)

__all__ = ["PhoneNumber"]


class PhoneNumber(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A DID owned by a tenant and routed to an agent (Phase 0 report §6,
    Entity: PhoneNumber). `agent_id`/`pinned_version_id` are nullable: a
    number can be claimed before it is assigned to an agent."""

    __tablename__ = "phone_numbers"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    e164: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    version_pin_mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="follow_published",
        server_default="follow_published",
    )
    pinned_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    inbound_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    outbound_caller_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ownership_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = tenant_table_args(
        UniqueConstraint("e164", name="uq_phone_numbers_e164_global"),
        UniqueConstraint("id", "tenant_id", name="uq_phone_numbers_id_tenant"),
        ForeignKeyConstraint(
            ["agent_id", "tenant_id"],
            ["app.agents.id", "app.agents.tenant_id"],
            name="fk_phone_numbers_agent",
        ),
        ForeignKeyConstraint(
            ["pinned_version_id", "tenant_id"],
            ["app.agent_versions.id", "app.agent_versions.tenant_id"],
            name="fk_phone_numbers_pinned_version",
        ),
        CheckConstraint(
            "version_pin_mode IN ('follow_published', 'pinned')",
            name="ck_phone_numbers_version_pin_mode",
        ),
        CheckConstraint(
            "(version_pin_mode = 'pinned') = (pinned_version_id IS NOT NULL)",
            name="ck_phone_numbers_pin_consistency",
        ),
        Index("ix_phone_numbers_tenant_id", "tenant_id"),
        Index("ix_phone_numbers_agent_id", "agent_id"),
    )
