"""create app.contacts, app.calendars, app.calendar_events; add call_sessions.contact_id

Revision ID: 0004_contacts_calendar
Revises: 0003_conversation_turns
Create Date: 2026-09-22

Phase 2.6 -- Contacts and a minimal internal calendar foundation, exactly as
specified by `docs/PHASE-2.6-STATUS.md`. Three new tables, plus one nullable
column added to the existing `app.call_sessions` (brief §4: "prefer a
nullable tenant-aware `contact_id`" -- no new association table, the existing
`CallSession` lifecycle is otherwise unchanged).

**Ordering**: `app.contacts` is created first (nothing depends on it, and
both `app.calendar_events` and the `call_sessions.contact_id` FK depend on
its `UNIQUE(id, tenant_id)`); `app.calendars` next; `app.calendar_events`
last, since it composite-FKs into both.

**`app.contacts.phone_e164` is tenant-locally unique**
(`UNIQUE(tenant_id, phone_e164)`) -- deliberately unlike
`app.phone_numbers.e164`'s global uniqueness (Phase 2.1): a DID is a
platform routing resource, a contact's phone number is not, so two tenants
independently having a contact who shares a number is ordinary data, not a
tenant-isolation failure. See `voiceagent/contacts/models.py`.

**Composite tenant-aware foreign keys** follow exactly the pattern `0002`'s
own docstring established: `(child_id, tenant_id) REFERENCES (parent.id,
parent.tenant_id)`, with a supporting `UNIQUE(id, tenant_id)` on each new
parent (`contacts`, `calendars`) this migration also creates.

**`call_sessions.contact_id`** is added via `ALTER TABLE` (the table already
exists, from `0002`) with its own composite FK into `app.contacts`, mirroring
`0003`'s "fix a real gap in an already-applied migration by adding to it
in a later one, never by editing history" discipline -- here there is no gap
to fix, just a new optional column on an existing table.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from infra.db.rls import tenant_rls_statements

revision: str = "0004_contacts_calendar"
down_revision: str | Sequence[str] | None = "0003_conversation_turns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_APP_ROLE = "saas_os_app"
_NEW_TABLES_IN_ORDER = ("contacts", "calendars", "calendar_events")


def _app_role() -> str:
    role = os.environ.get("APP_DB_USER", _DEFAULT_APP_ROLE)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
        raise ValueError(f"APP_DB_USER must be a plain SQL identifier, got: {role!r}")
    return role


def _grant(table: str, app_role: str) -> None:
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO "{app_role}"')


def upgrade() -> None:
    app_role = _app_role()
    now = sa.func.now()

    # --- 1: app.contacts -----------------------------------------------
    op.create_table(
        "contacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("phone_e164", sa.String(length=20), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=now,
            onupdate=now,
            nullable=False,
        ),
        sa.UniqueConstraint("tenant_id", "phone_e164", name="uq_contacts_tenant_phone"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_contacts_id_tenant"),
        sa.CheckConstraint(
            r"phone_e164 ~ '^\+[1-9][0-9]{1,14}$'", name="ck_contacts_phone_e164_format"
        ),
        schema="app",
    )
    op.create_index("ix_contacts_tenant_id", "contacts", ["tenant_id"], schema="app")
    op.create_index(
        "ix_contacts_tenant_phone", "contacts", ["tenant_id", "phone_e164"], schema="app"
    )

    # --- 2: app.calendars -------------------------------------------------
    op.create_table(
        "calendars",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=now,
            onupdate=now,
            nullable=False,
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_calendars_id_tenant"),
        schema="app",
    )
    op.create_index("ix_calendars_tenant_id", "calendars", ["tenant_id"], schema="app")

    # --- 3: app.calendar_events --------------------------------------------
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("calendar_id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'scheduled'")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=now,
            onupdate=now,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["calendar_id", "tenant_id"],
            ["app.calendars.id", "app.calendars.tenant_id"],
            name="fk_calendar_events_calendar",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id", "tenant_id"],
            ["app.contacts.id", "app.contacts.tenant_id"],
            name="fk_calendar_events_contact",
        ),
        sa.CheckConstraint("start_at < end_at", name="ck_calendar_events_interval"),
        sa.CheckConstraint(
            "status IN ('scheduled', 'cancelled')", name="ck_calendar_events_status"
        ),
        schema="app",
    )
    op.create_index("ix_calendar_events_tenant_id", "calendar_events", ["tenant_id"], schema="app")
    op.create_index(
        "ix_calendar_events_calendar_id", "calendar_events", ["calendar_id"], schema="app"
    )
    op.create_index(
        "ix_calendar_events_contact_id", "calendar_events", ["contact_id"], schema="app"
    )
    op.create_index(
        "ix_calendar_events_calendar_window",
        "calendar_events",
        ["calendar_id", "start_at", "end_at"],
        schema="app",
    )

    # --- 4: call_sessions.contact_id (brief §4) ----------------------------
    op.add_column("call_sessions", sa.Column("contact_id", sa.Uuid(), nullable=True), schema="app")
    op.create_foreign_key(
        "fk_call_sessions_contact",
        "call_sessions",
        "contacts",
        ["contact_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema="app",
        referent_schema="app",
    )
    op.create_index("ix_call_sessions_contact_id", "call_sessions", ["contact_id"], schema="app")

    # --- 5: Row-Level Security, the three new tables -----------------------
    for table in _NEW_TABLES_IN_ORDER:
        for statement in tenant_rls_statements(table, schema="app"):
            op.execute(statement)

    # --- 6: grants -----------------------------------------------------------
    for table in _NEW_TABLES_IN_ORDER:
        _grant(table, app_role)


def downgrade() -> None:
    app_role = _app_role()

    op.drop_index("ix_call_sessions_contact_id", table_name="call_sessions", schema="app")
    op.drop_constraint(
        "fk_call_sessions_contact", "call_sessions", schema="app", type_="foreignkey"
    )
    op.drop_column("call_sessions", "contact_id", schema="app")

    for table in reversed(_NEW_TABLES_IN_ORDER):
        op.execute(f'REVOKE SELECT, INSERT, UPDATE, DELETE ON app.{table} FROM "{app_role}"')

    op.drop_table("calendar_events", schema="app")
    op.drop_table("calendars", schema="app")
    op.drop_table("contacts", schema="app")
