"""create app.agents, app.agent_versions, app.phone_numbers, app.call_sessions

Revision ID: 0002_domain_foundation
Revises: 0001_app_schema
Create Date: 2026-09-21

The Phase 2.1 domain foundation, exactly as specified by
`docs/PHASE-2.0-ARCHITECTURE.md` §23. Four tables only -- no
`conversations`, `contacts`, `tools`, `workflows`, `recordings`,
`provider_credentials`, or `runtime_assignments` (Phase 2.1 brief §3, §26):
those are deferred, and creating one ahead of the code that would populate it
is exactly the mistake `0001`'s own docstring already declined to make.

**Ordering** (Phase 2.0 report §23.1) exists to resolve one real circular
reference: `app.agents.draft_version_id`/`published_version_id` point at
`app.agent_versions`, and `app.agent_versions.agent_id` points back at
`app.agents`. Both directions cannot be created in the same `CREATE TABLE`
statement, so `agents` is created first, without those two FKs; they are
added via `ALTER TABLE` in step 5, after `agent_versions` exists.

**Composite tenant-aware foreign keys** (`(child_id, tenant_id) REFERENCES
(parent.id, parent.tenant_id)`, each parent additionally carrying
`UNIQUE(id, tenant_id)`) are applied to every tenant-owned parent/child
relationship here -- the mechanism that makes a cross-tenant reference a
constraint violation rather than merely an RLS-hidden row (Phase 2.1 brief
§8; Phase 2.0 report §15, mirroring SaaS-OS's own
`core.rbac.ServiceAccountRole` pattern).

**`app.phone_numbers.e164` is globally unique** (`UNIQUE(e164)`, not
`UNIQUE(tenant_id, e164)`) -- deliberately, per Phase 0 report §14.1 and
Phase 2.0 report §15.1: two tenants claiming the same DID is a
tenant-isolation failure. See `voiceagent/phone_numbers/service.py` for the
required generic-conflict handling this implies at the application layer.

**The immutability trigger** (`app.forbid_published_agent_version_update()`)
implements ADR-0004 at the database layer, restricted to exactly one legal
transition on an already-`published` row (`published -> archived`, with
every other column unchanged). It is a **correction** of the exact SQL given
in Phase 2.0 report §23.6: that draft compared `NEW.config = OLD.config`
directly, but PostgreSQL's `json` type (unlike `jsonb`) has no equality
operator at all -- `json = json` raises `operator does not exist: json =
json`, which would have made every `published -> archived` transition fail
with a database error, not merely be rejected. This migration casts both
sides to `text` for that one comparison instead; verified against a real
PostgreSQL instance as part of this phase's own testing (see
`docs/PHASE-2.1-STATUS.md`, "Deviations"). No other part of the Phase 2.0
contract needed correction.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from infra.db.rls import tenant_rls_statements

revision: str = "0002_domain_foundation"
down_revision: str | Sequence[str] | None = "0001_app_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_APP_ROLE = "saas_os_app"
_TABLES_IN_ORDER = ("agents", "agent_versions", "phone_numbers", "call_sessions")


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

    # --- 1: app.agents, without the two FKs into agent_versions yet -------
    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column("draft_version_id", sa.Uuid(), nullable=True),
        sa.Column("published_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=now,
            onupdate=now,
            nullable=False,
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_name"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_agents_id_tenant"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_agents_status"),
        schema="app",
    )
    op.create_index("ix_agents_tenant_id", "agents", ["tenant_id"], schema="app")

    # --- 2: app.agent_versions ---------------------------------------------
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'draft'")
        ),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.Uuid(), nullable=True),  # no FK -- S23 note above
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=now,
            onupdate=now,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_id", "tenant_id"],
            ["app.agents.id", "app.agents.tenant_id"],
            name="fk_agent_versions_agent",
        ),
        sa.UniqueConstraint(
            "agent_id", "version_number", name="uq_agent_versions_agent_version_number"
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_agent_versions_id_tenant"),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name="ck_agent_versions_status"
        ),
        sa.CheckConstraint(
            "config_hash ~ '^[0-9a-f]{64}$'", name="ck_agent_versions_config_hash_format"
        ),
        sa.CheckConstraint(
            "status <> 'published' OR published_at IS NOT NULL",
            name="ck_agent_versions_published_at_required",
        ),
        schema="app",
    )
    op.create_index("ix_agent_versions_tenant_id", "agent_versions", ["tenant_id"], schema="app")
    op.create_index(
        "ix_agent_versions_agent_status", "agent_versions", ["agent_id", "status"], schema="app"
    )

    # --- 3: the two FKs on app.agents that could not exist until step 2 ---
    op.create_foreign_key(
        "fk_agents_draft_version",
        "agents",
        "agent_versions",
        ["draft_version_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema="app",
        referent_schema="app",
    )
    op.create_foreign_key(
        "fk_agents_published_version",
        "agents",
        "agent_versions",
        ["published_version_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema="app",
        referent_schema="app",
    )

    # --- 4: app.phone_numbers ----------------------------------------------
    op.create_table(
        "phone_numbers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("e164", sa.String(length=20), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column(
            "version_pin_mode",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'follow_published'"),
        ),
        sa.Column("pinned_version_id", sa.Uuid(), nullable=True),
        sa.Column("inbound_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("outbound_caller_id", sa.String(length=20), nullable=True),
        sa.Column("ownership_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=now,
            onupdate=now,
            nullable=False,
        ),
        # GLOBAL uniqueness -- deliberately not (tenant_id, e164).
        sa.UniqueConstraint("e164", name="uq_phone_numbers_e164_global"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_phone_numbers_id_tenant"),
        sa.ForeignKeyConstraint(
            ["agent_id", "tenant_id"],
            ["app.agents.id", "app.agents.tenant_id"],
            name="fk_phone_numbers_agent",
        ),
        sa.ForeignKeyConstraint(
            ["pinned_version_id", "tenant_id"],
            ["app.agent_versions.id", "app.agent_versions.tenant_id"],
            name="fk_phone_numbers_pinned_version",
        ),
        sa.CheckConstraint(
            "version_pin_mode IN ('follow_published', 'pinned')",
            name="ck_phone_numbers_version_pin_mode",
        ),
        sa.CheckConstraint(
            "(version_pin_mode = 'pinned') = (pinned_version_id IS NOT NULL)",
            name="ck_phone_numbers_pin_consistency",
        ),
        schema="app",
    )
    op.create_index("ix_phone_numbers_tenant_id", "phone_numbers", ["tenant_id"], schema="app")
    op.create_index("ix_phone_numbers_agent_id", "phone_numbers", ["agent_id"], schema="app")

    # --- 5: app.call_sessions -----------------------------------------------
    op.create_table(
        "call_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'initiated'")
        ),
        sa.Column("from_e164", sa.String(length=20), nullable=False),
        sa.Column("to_e164", sa.String(length=20), nullable=False),
        sa.Column("phone_number_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("fs_channel_uuid", sa.String(length=64), nullable=True),
        sa.Column("runtime_instance_id", sa.String(length=200), nullable=True),
        sa.Column("runtime_assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("hangup_cause", sa.String(length=30), nullable=True),
        sa.Column("end_reason", sa.String(length=30), nullable=True),
        sa.Column("data_authorization_decision_id", sa.Uuid(), nullable=True),  # no FK
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=now,
            onupdate=now,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["phone_number_id", "tenant_id"],
            ["app.phone_numbers.id", "app.phone_numbers.tenant_id"],
            name="fk_call_sessions_phone_number",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id", "tenant_id"],
            ["app.agents.id", "app.agents.tenant_id"],
            name="fk_call_sessions_agent",
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id", "tenant_id"],
            ["app.agent_versions.id", "app.agent_versions.tenant_id"],
            name="fk_call_sessions_agent_version",
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="ck_call_sessions_direction"
        ),
        sa.CheckConstraint(
            "status IN ('initiated', 'ringing', 'answered', 'in_progress', "
            "'completed', 'failed', 'interrupted')",
            name="ck_call_sessions_status",
        ),
        schema="app",
    )
    op.create_index("ix_call_sessions_tenant_id", "call_sessions", ["tenant_id"], schema="app")
    op.create_index(
        "ix_call_sessions_phone_number_id", "call_sessions", ["phone_number_id"], schema="app"
    )
    op.create_index(
        "ix_call_sessions_agent_version_id", "call_sessions", ["agent_version_id"], schema="app"
    )
    op.create_index("ix_call_sessions_status", "call_sessions", ["status"], schema="app")
    op.create_index(
        "ix_call_sessions_runtime_instance_id",
        "call_sessions",
        ["runtime_instance_id"],
        schema="app",
    )
    op.create_index(
        "ix_call_sessions_fs_channel_uuid", "call_sessions", ["fs_channel_uuid"], schema="app"
    )

    # --- 6: Row-Level Security, all four tables -----------------------------
    for table in _TABLES_IN_ORDER:
        for statement in tenant_rls_statements(table, schema="app"):
            op.execute(statement)

    # --- 7: grants -----------------------------------------------------------
    # Redundant with 0001's `ALTER DEFAULT PRIVILEGES` for a table created by
    # the same role that ran that migration, and harmless if so (GRANT is not
    # an error to repeat) -- kept explicit so this migration is correct on
    # its own even if that assumption about migration-role continuity ever
    # stops holding.
    for table in _TABLES_IN_ORDER:
        _grant(table, app_role)

    # --- 8: AgentVersion immutability trigger (ADR-0004) --------------------
    # See module docstring: this is a corrected version of Phase 2.0 report
    # S23.6's draft SQL -- `NEW.config = OLD.config` does not compile against
    # PostgreSQL's `json` type (only `jsonb` supports `=`); both sides are
    # cast to `text` here instead.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.forbid_published_agent_version_update()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'published' THEN
                IF NEW.status = 'archived'
                   AND NEW.id = OLD.id
                   AND NEW.tenant_id = OLD.tenant_id
                   AND NEW.agent_id = OLD.agent_id
                   AND NEW.version_number = OLD.version_number
                   AND NEW.config::text = OLD.config::text
                   AND NEW.config_hash = OLD.config_hash
                   AND NEW.published_at = OLD.published_at
                   AND NEW.published_by IS NOT DISTINCT FROM OLD.published_by
                   AND NEW.created_at = OLD.created_at
                THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION
                    'app.agent_versions row % is published and immutable (ADR-0004)',
                    OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_versions_immutable
            BEFORE UPDATE ON app.agent_versions
            FOR EACH ROW
            EXECUTE FUNCTION app.forbid_published_agent_version_update();
        """
    )


def downgrade() -> None:
    app_role = _app_role()

    op.execute("DROP TRIGGER IF EXISTS agent_versions_immutable ON app.agent_versions")
    op.execute("DROP FUNCTION IF EXISTS app.forbid_published_agent_version_update()")

    for table in reversed(_TABLES_IN_ORDER):
        op.execute(f'REVOKE SELECT, INSERT, UPDATE, DELETE ON app.{table} FROM "{app_role}"')

    op.drop_table("call_sessions", schema="app")
    op.drop_table("phone_numbers", schema="app")
    op.drop_constraint("fk_agents_draft_version", "agents", schema="app", type_="foreignkey")
    op.drop_constraint("fk_agents_published_version", "agents", schema="app", type_="foreignkey")
    op.drop_table("agent_versions", schema="app")
    op.drop_table("agents", schema="app")
