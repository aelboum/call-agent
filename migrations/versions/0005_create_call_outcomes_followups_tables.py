"""create app.call_outcomes, app.follow_up_actions

Revision ID: 0005_call_outcomes_followups
Revises: 0004_contacts_calendar
Create Date: 2026-09-22

Phase 2.7 -- Call Outcomes & Follow-up Primitives, exactly as specified by
`docs/PHASE-2.7-STATUS.md`. Two new tables only -- no workflow table, no
event-bus table, no generic task table (brief §18/§26).

**Step 0 fixes a real gap in `0004`**, exactly the same "a later migration
adds the missing constraint a new child needed, never by editing history"
pattern `0003`'s own docstring already established for `call_sessions`:
`0004` gave `contacts` and `calendars` each a `UNIQUE(id, tenant_id)` (the
mechanism a composite tenant-aware FK requires of the table it points at),
but never gave one to `calendar_events`, because nothing referenced it as a
parent until now (`follow_up_actions.calendar_event_id`). `0004` itself is
already-applied, committed history (Phase 2.6) and is not edited
retroactively; this migration adds the missing constraint as its own first
step instead.

**`call_outcomes`** carries `UNIQUE(call_session_id)` -- at most one current
outcome per call (brief §6); a changed business result updates this row,
never a new historical one.

**`follow_up_actions`** carries
`CHECK (type = 'appointment') = (calendar_event_id IS NOT NULL)` -- the
deterministic relationship rule brief §8 asks for, enforced at the database
layer in addition to `voiceagent.followups.service.create_follow_up()`'s own
application-level check (this product's established "CHECK plus
application validation, never one alone" discipline -- e.g.
`ck_phone_numbers_pin_consistency`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from infra.db.rls import tenant_rls_statements

revision: str = "0005_call_outcomes_followups"
down_revision: str | Sequence[str] | None = "0004_contacts_calendar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_APP_ROLE = "saas_os_app"
_NEW_TABLES_IN_ORDER = ("call_outcomes", "follow_up_actions")


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

    # --- 0: the missing uq_calendar_events_id_tenant, see module docstring -
    op.create_unique_constraint(
        "uq_calendar_events_id_tenant", "calendar_events", ["id", "tenant_id"], schema="app"
    )

    # --- 1: app.call_outcomes -----------------------------------------------
    op.create_table(
        "call_outcomes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("call_session_id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=now,
            onupdate=now,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["call_session_id", "tenant_id"],
            ["app.call_sessions.id", "app.call_sessions.tenant_id"],
            name="fk_call_outcomes_call_session",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id", "tenant_id"],
            ["app.contacts.id", "app.contacts.tenant_id"],
            name="fk_call_outcomes_contact",
        ),
        sa.UniqueConstraint("call_session_id", name="uq_call_outcomes_call_session"),
        sa.CheckConstraint(
            "outcome IN ('resolved', 'appointment_scheduled', 'follow_up_required', "
            "'no_answer', 'wrong_number', 'not_interested')",
            name="ck_call_outcomes_outcome",
        ),
        schema="app",
    )
    op.create_index("ix_call_outcomes_tenant_id", "call_outcomes", ["tenant_id"], schema="app")
    op.create_index("ix_call_outcomes_contact_id", "call_outcomes", ["contact_id"], schema="app")

    # --- 2: app.follow_up_actions --------------------------------------------
    op.create_table(
        "follow_up_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("call_session_id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("calendar_event_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=now,
            onupdate=now,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["call_session_id", "tenant_id"],
            ["app.call_sessions.id", "app.call_sessions.tenant_id"],
            name="fk_follow_up_actions_call_session",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id", "tenant_id"],
            ["app.contacts.id", "app.contacts.tenant_id"],
            name="fk_follow_up_actions_contact",
        ),
        sa.ForeignKeyConstraint(
            ["calendar_event_id", "tenant_id"],
            ["app.calendar_events.id", "app.calendar_events.tenant_id"],
            name="fk_follow_up_actions_calendar_event",
        ),
        sa.CheckConstraint(
            "type IN ('appointment', 'contact', 'manual_follow_up')",
            name="ck_follow_up_actions_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'cancelled')",
            name="ck_follow_up_actions_status",
        ),
        sa.CheckConstraint(
            "(type = 'appointment') = (calendar_event_id IS NOT NULL)",
            name="ck_follow_up_actions_appointment_requires_calendar_event",
        ),
        schema="app",
    )
    op.create_index(
        "ix_follow_up_actions_tenant_id", "follow_up_actions", ["tenant_id"], schema="app"
    )
    op.create_index(
        "ix_follow_up_actions_call_session_id",
        "follow_up_actions",
        ["call_session_id"],
        schema="app",
    )
    op.create_index(
        "ix_follow_up_actions_contact_id", "follow_up_actions", ["contact_id"], schema="app"
    )
    op.create_index(
        "ix_follow_up_actions_calendar_event_id",
        "follow_up_actions",
        ["calendar_event_id"],
        schema="app",
    )
    op.create_index("ix_follow_up_actions_status", "follow_up_actions", ["status"], schema="app")

    # --- 3: Row-Level Security, the two new tables ---------------------------
    for table in _NEW_TABLES_IN_ORDER:
        for statement in tenant_rls_statements(table, schema="app"):
            op.execute(statement)

    # --- 4: grants -------------------------------------------------------------
    for table in _NEW_TABLES_IN_ORDER:
        _grant(table, app_role)


def downgrade() -> None:
    app_role = _app_role()

    for table in reversed(_NEW_TABLES_IN_ORDER):
        op.execute(f'REVOKE SELECT, INSERT, UPDATE, DELETE ON app.{table} FROM "{app_role}"')

    op.drop_table("follow_up_actions", schema="app")
    op.drop_table("call_outcomes", schema="app")
    op.drop_constraint(
        "uq_calendar_events_id_tenant", "calendar_events", schema="app", type_="unique"
    )
