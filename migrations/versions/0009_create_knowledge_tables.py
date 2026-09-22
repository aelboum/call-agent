"""create app.knowledge_sources, app.knowledge_items

Revision ID: 0009_knowledge_tables
Revises: 0008_call_workflow_executions
Create Date: 2026-09-22

Phase 2.11 -- Agent Knowledge & Context, exactly as specified by
`docs/PHASE-2.11-STATUS.md`. Two tables only -- no vector store, no generic
document/memory table (`voiceagent.knowledge.models`'s own module docstring).

**`app.forbid_knowledge_item_content_update`** mirrors
`app.forbid_published_agent_version_update` (migration `0002`): once a
`knowledge_items` row leaves `status='draft'`, `title`/`content` (and
`source_id`/`tenant_id`) can never change again, and the only further status
transition permitted is `active -> archived`. This is what lets
`AgentVersion.config["knowledge"]["item_ids"]` reference items by id alone
and stay deterministic for the version's entire lifetime (ADR-0004 extended
-- see `voiceagent.knowledge` package docstring).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from infra.db.rls import tenant_rls_statements

revision: str = "0009_knowledge_tables"
down_revision: str | Sequence[str] | None = "0008_call_workflow_executions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCES_TABLE = "knowledge_sources"
_ITEMS_TABLE = "knowledge_items"
_MAX_ITEM_CONTENT_LENGTH = 20_000
_DEFAULT_APP_ROLE = "saas_os_app"


def _app_role() -> str:
    role = os.environ.get("APP_DB_USER", _DEFAULT_APP_ROLE)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
        raise ValueError(f"APP_DB_USER must be a plain SQL identifier, got: {role!r}")
    return role


def upgrade() -> None:
    app_role = _app_role()
    now = sa.func.now()

    op.create_table(
        _SOURCES_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=now,
            onupdate=now,
            nullable=False,
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_knowledge_sources_id_tenant"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_knowledge_sources_tenant_name"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_knowledge_sources_status"),
        schema="app",
    )
    op.create_index("ix_knowledge_sources_tenant_id", _SOURCES_TABLE, ["tenant_id"], schema="app")
    op.create_index("ix_knowledge_sources_status", _SOURCES_TABLE, ["status"], schema="app")

    op.create_table(
        _ITEMS_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'draft'")
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
            ["source_id", "tenant_id"],
            ["app.knowledge_sources.id", "app.knowledge_sources.tenant_id"],
            name="fk_knowledge_items_source",
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_knowledge_items_id_tenant"),
        sa.UniqueConstraint(
            "tenant_id", "source_id", "title", name="uq_knowledge_items_tenant_source_title"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'archived')", name="ck_knowledge_items_status"
        ),
        sa.CheckConstraint(
            f"length(content) <= {_MAX_ITEM_CONTENT_LENGTH}",
            name="ck_knowledge_items_content_length",
        ),
        schema="app",
    )
    op.create_index("ix_knowledge_items_tenant_id", _ITEMS_TABLE, ["tenant_id"], schema="app")
    op.create_index("ix_knowledge_items_source_id", _ITEMS_TABLE, ["source_id"], schema="app")
    op.create_index(
        "ix_knowledge_items_tenant_status", _ITEMS_TABLE, ["tenant_id", "status"], schema="app"
    )

    for table in (_SOURCES_TABLE, _ITEMS_TABLE):
        for statement in tenant_rls_statements(table, schema="app"):
            op.execute(statement)

    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON app.{_SOURCES_TABLE} TO "{app_role}"')
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON app.{_ITEMS_TABLE} TO "{app_role}"')

    # -- KnowledgeItem content-immutability trigger (ADR-0004 extended) ------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.forbid_knowledge_item_content_update()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.status <> 'draft' THEN
                IF NEW.id = OLD.id
                   AND NEW.tenant_id = OLD.tenant_id
                   AND NEW.source_id = OLD.source_id
                   AND NEW.title = OLD.title
                   AND NEW.content = OLD.content
                   AND NEW.created_at = OLD.created_at
                   AND (NEW.status = OLD.status
                        OR (OLD.status = 'active' AND NEW.status = 'archived'))
                THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION
                    'app.knowledge_items row % is % and its content is immutable',
                    OLD.id, OLD.status;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_items_content_immutable
            BEFORE UPDATE ON app.knowledge_items
            FOR EACH ROW
            EXECUTE FUNCTION app.forbid_knowledge_item_content_update();
        """
    )


def downgrade() -> None:
    app_role = _app_role()

    op.execute("DROP TRIGGER IF EXISTS knowledge_items_content_immutable ON app.knowledge_items")
    op.execute("DROP FUNCTION IF EXISTS app.forbid_knowledge_item_content_update()")

    op.execute(f'REVOKE SELECT, INSERT, UPDATE, DELETE ON app.{_ITEMS_TABLE} FROM "{app_role}"')
    op.execute(f'REVOKE SELECT, INSERT, UPDATE, DELETE ON app.{_SOURCES_TABLE} FROM "{app_role}"')

    op.drop_table(_ITEMS_TABLE, schema="app")
    op.drop_table(_SOURCES_TABLE, schema="app")
