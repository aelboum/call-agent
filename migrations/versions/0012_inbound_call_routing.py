"""add app.inbound_call_routes and a unique constraint on
app.call_sessions.fs_channel_uuid

Revision ID: 0012_inbound_routing
Revises: 0011_call_sessions_index
Create Date: 2026-09-27

Phase 2.22 (Call Orchestrator) needs two things this schema did not yet
have:

**1. A `fs_channel_uuid` -> `CallSession` uniqueness guarantee.** The column
already existed (`0002`), but only with a plain, non-unique index -- nothing
stopped two rows from carrying the same value. A real FreeSWITCH ESL
reconnect (Phase 2.21) can and does replay events; without a database-level
uniqueness guarantee, a replayed `OFFERED` event handled by two racing
orchestrator processes (or twice by one) could create two `CallSession` rows
for what is really one external call. This migration adds a **partial
unique index** (`WHERE fs_channel_uuid IS NOT NULL`) -- partial because
outbound calls may go through a window before a channel UUID is known, and
NULL must stay unconstrained (SQL's own `NULL <> NULL` semantics would make
a plain `UNIQUE` constraint permit that regardless, but a partial index
states the intent explicitly and matches this column's own nullable
definition). This replaces the old plain index (`ix_call_sessions_fs_
channel_uuid`) with a unique one of the same shape -- no application code
that queried by this column needs to change, only inserts of a genuine
duplicate now fail instead of silently succeeding.

**2. An explicitly-not-row-level-secured routing lookup.** Phase 2.22's own
security review confirmed there is no existing way to resolve "which tenant
owns this phone number" before a `TenantContext` exists (`voiceagent.calls
.routing`'s own module docstring explains the full reasoning) --
`app.phone_numbers` is `FORCE ROW LEVEL SECURITY`'d exactly like every
other tenant-owned table (`0002`'s own `tenant_rls_statements()` call), and
with no `app.tenant_id` session variable set (the situation an inbound call
starts in, by definition), that policy's own `tenant_id = NULLIF(...,
'')::uuid` comparison is `NULL`-valued and therefore matches nothing --
correctly safe, but useless for the one legitimate case that needs to ask
"which tenant, if any, owns this DID" *before* any tenant is known.

`app.inbound_call_routes` is a small, deliberately-not-RLS'd mirror of the
exact four non-sensitive columns an inbound-call router needs
(`tenant_id`, `phone_number_id`, `e164`, `agent_id`, `inbound_enabled`) --
never a call's content, never a tenant's other configuration, never
anything a real telephone switch would not already need to know to route a
call at all. It is kept in sync with `app.phone_numbers` by a trigger
(`app.sync_inbound_call_route()`), not by any application code path -- so
`voiceagent.phone_numbers.service.register_phone_number()`/
`update_phone_number()` needed no changes, and neither would any future
write path to `phone_numbers` that does not yet exist. This is the
"smallest explicit routing primitive" Phase 2.22's own brief authorized
introducing when no sufficient existing mechanism was found (section 5).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_inbound_routing"
down_revision: str | Sequence[str] | None = "0011_call_sessions_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_APP_ROLE = "saas_os_app"


def _app_role() -> str:
    role = os.environ.get("APP_DB_USER", _DEFAULT_APP_ROLE)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
        raise ValueError(f"APP_DB_USER must be a plain SQL identifier, got: {role!r}")
    return role


def upgrade() -> None:
    app_role = _app_role()

    # --- 1: fs_channel_uuid becomes uniquely indexed (partial, NULL-safe) --
    op.drop_index("ix_call_sessions_fs_channel_uuid", table_name="call_sessions", schema="app")
    op.create_index(
        "uq_call_sessions_fs_channel_uuid",
        "call_sessions",
        ["fs_channel_uuid"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("fs_channel_uuid IS NOT NULL"),
    )

    # --- 2: app.inbound_call_routes -- deliberately NOT row-level-secured --
    op.create_table(
        "inbound_call_routes",
        sa.Column("phone_number_id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("e164", sa.String(length=20), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("inbound_enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("e164", name="uq_inbound_call_routes_e164"),
        schema="app",
    )
    op.create_index(
        "ix_inbound_call_routes_tenant_id", "inbound_call_routes", ["tenant_id"], schema="app"
    )
    # No RLS here -- see module docstring. `phone_numbers` itself, and every
    # other tenant-owned table, keeps FORCE ROW LEVEL SECURITY unchanged.

    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON app.inbound_call_routes TO "{app_role}"')

    # Backfill from whatever phone_numbers rows already exist.
    op.execute(
        """
        INSERT INTO app.inbound_call_routes
            (phone_number_id, tenant_id, e164, agent_id, inbound_enabled, updated_at)
        SELECT id, tenant_id, e164, agent_id, inbound_enabled, now()
        FROM app.phone_numbers
        ON CONFLICT (phone_number_id) DO NOTHING
        """
    )

    # --- 3: trigger keeping it in sync with app.phone_numbers --------------
    # SECURITY INVOKER (the default -- no SECURITY DEFINER clause): runs as
    # whichever role performed the INSERT/UPDATE/DELETE on phone_numbers,
    # which in production is always `app_role` -- the identical GRANT above
    # is exactly what that requires, no elevated privilege is introduced.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.sync_inbound_call_route()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                DELETE FROM app.inbound_call_routes WHERE phone_number_id = OLD.id;
                RETURN OLD;
            END IF;
            INSERT INTO app.inbound_call_routes
                (phone_number_id, tenant_id, e164, agent_id, inbound_enabled, updated_at)
            VALUES (NEW.id, NEW.tenant_id, NEW.e164, NEW.agent_id, NEW.inbound_enabled, now())
            ON CONFLICT (phone_number_id) DO UPDATE SET
                tenant_id = EXCLUDED.tenant_id,
                e164 = EXCLUDED.e164,
                agent_id = EXCLUDED.agent_id,
                inbound_enabled = EXCLUDED.inbound_enabled,
                updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER phone_numbers_sync_inbound_route
            AFTER INSERT OR UPDATE OR DELETE ON app.phone_numbers
            FOR EACH ROW
            EXECUTE FUNCTION app.sync_inbound_call_route();
        """
    )


def downgrade() -> None:
    app_role = _app_role()

    op.execute("DROP TRIGGER IF EXISTS phone_numbers_sync_inbound_route ON app.phone_numbers")
    op.execute("DROP FUNCTION IF EXISTS app.sync_inbound_call_route()")
    op.execute(
        f'REVOKE SELECT, INSERT, UPDATE, DELETE ON app.inbound_call_routes FROM "{app_role}"'
    )
    op.drop_table("inbound_call_routes", schema="app")

    op.drop_index("uq_call_sessions_fs_channel_uuid", table_name="call_sessions", schema="app")
    op.create_index(
        "ix_call_sessions_fs_channel_uuid", "call_sessions", ["fs_channel_uuid"], schema="app"
    )
