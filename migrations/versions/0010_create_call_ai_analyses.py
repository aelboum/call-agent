"""create app.call_ai_analyses

Revision ID: 0010_call_ai_analyses
Revises: 0009_knowledge_tables
Create Date: 2026-09-22

Phase 2.12 -- Advanced AI Post-Call Intelligence, exactly as specified by
`docs/PHASE-2.12-STATUS.md`. One table only -- no generic `jobs`/`events`
table, no vector column, no second transcript/history table (the transcript
this phase reads remains `app.conversation_turns`, Phase 2.5's own table).

**One row per analysis *version*, not one row per call**
(`UNIQUE(call_session_id, version)`, not `UNIQUE(call_session_id)`) -- the
versioning strategy `voiceagent.call_intelligence.models`'s own module
docstring explains: a rebuild always inserts a new row rather than
overwriting a completed one.

**`app.forbid_call_ai_analysis_completed_update`** mirrors
`app.forbid_published_agent_version_update` (migration `0002`) and
`app.forbid_knowledge_item_content_update` (migration `0009`): once a row
reaches `status='completed'`, no column may ever change again -- "do not
mutate historical results in place" enforced at the database layer, not
merely by application discipline.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from infra.db.rls import tenant_rls_statements

revision: str = "0010_call_ai_analyses"
down_revision: str | Sequence[str] | None = "0009_knowledge_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "call_ai_analyses"
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
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("call_session_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("prompt_version", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=50), nullable=True),
        sa.Column("result", sa.JSON(none_as_null=True), nullable=True),
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
            name="fk_call_ai_analyses_call_session",
        ),
        sa.UniqueConstraint(
            "call_session_id", "version", name="uq_call_ai_analyses_call_session_version"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_call_ai_analyses_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_call_ai_analyses_version_positive"),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_call_ai_analyses_attempt_count_non_negative"
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN ('privacy_denied', "
            "'call_not_completed', 'provider_timeout', 'provider_error', "
            "'provider_unavailable', 'malformed_response', 'unexpected_error')",
            name="ck_call_ai_analyses_failure_reason",
        ),
        sa.CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL AND result IS NOT NULL)",
            name="ck_call_ai_analyses_completed_iff_result",
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)",
            name="ck_call_ai_analyses_failed_iff_reason",
        ),
        schema="app",
    )
    op.create_index("ix_call_ai_analyses_tenant_id", _TABLE, ["tenant_id"], schema="app")
    op.create_index(
        "ix_call_ai_analyses_call_session_id", _TABLE, ["call_session_id"], schema="app"
    )
    op.create_index("ix_call_ai_analyses_status", _TABLE, ["status"], schema="app")
    op.create_index(
        "ix_call_ai_analyses_claim_lookup",
        _TABLE,
        ["tenant_id", "next_attempt_at"],
        schema="app",
        postgresql_where=sa.text("next_attempt_at IS NOT NULL"),
    )

    for statement in tenant_rls_statements(_TABLE, schema="app"):
        op.execute(statement)

    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON app.{_TABLE} TO "{app_role}"')

    # -- CallAiAnalysis completed-immutability trigger (ADR-0004 extended) --
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.forbid_call_ai_analysis_completed_update()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'completed' THEN
                RAISE EXCEPTION
                    'app.call_ai_analyses row % is completed and immutable',
                    OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER call_ai_analyses_completed_immutable
            BEFORE UPDATE ON app.call_ai_analyses
            FOR EACH ROW
            EXECUTE FUNCTION app.forbid_call_ai_analysis_completed_update();
        """
    )


def downgrade() -> None:
    app_role = _app_role()

    op.execute(
        "DROP TRIGGER IF EXISTS call_ai_analyses_completed_immutable ON app.call_ai_analyses"
    )
    op.execute("DROP FUNCTION IF EXISTS app.forbid_call_ai_analysis_completed_update()")

    op.execute(f'REVOKE SELECT, INSERT, UPDATE, DELETE ON app.{_TABLE} FROM "{app_role}"')
    op.drop_table(_TABLE, schema="app")
