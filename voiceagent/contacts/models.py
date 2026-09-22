"""`app.contacts` (Phase 2.6 brief §9).

`phone_e164` is **tenant-locally unique** (`UNIQUE(tenant_id, phone_e164)`) --
deliberately unlike `voiceagent.phone_numbers.models.PhoneNumber.e164`, which
is globally unique because two tenants claiming the same DID is an inbound-
routing ambiguity. A contact's phone number carries no such platform-level
routing meaning: two different tenants each having a contact who shares a
phone number (e.g. two businesses that both serve the same customer) is
ordinary, expected data, not a tenant-isolation failure.
"""

from __future__ import annotations

import uuid

from voiceagent.db import (
    Base,
    CheckConstraint,
    ForeignKey,
    Index,
    Mapped,
    String,
    TimestampMixin,
    UniqueConstraint,
    UUIDPrimaryKeyMixin,
    mapped_column,
    tenant_table_args,
)

__all__ = ["Contact"]


class Contact(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A tenant's contact (Phase 2.6 brief §9). Created only through
    `voiceagent.contacts.service.create_contact()` -- there is no automatic
    contact creation anywhere in this product (brief §9: "do not add
    automatic contact creation")."""

    __tablename__ = "contacts"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    __table_args__ = tenant_table_args(
        UniqueConstraint("tenant_id", "phone_e164", name="uq_contacts_tenant_phone"),
        UniqueConstraint("id", "tenant_id", name="uq_contacts_id_tenant"),
        CheckConstraint(
            r"phone_e164 ~ '^\+[1-9][0-9]{1,14}$'", name="ck_contacts_phone_e164_format"
        ),
        Index("ix_contacts_tenant_id", "tenant_id"),
        Index("ix_contacts_tenant_phone", "tenant_id", "phone_e164"),
    )
