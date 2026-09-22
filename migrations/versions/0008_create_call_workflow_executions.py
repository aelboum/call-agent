"""create app.call_workflow_executions

Revision ID: 0008_call_workflow_executions
Revises: 0007_follow_up_execution
Create Date: 2026-09-22

Phase 2.10 -- Controlled Call Workflows, exactly as specified by
`docs/PHASE-2.10-STATUS.md`. One table only -- no generic `jobs`/`tasks`/
`events`/scheduler table, no second `workflows`/`workflow_versions` table
(the workflow *definition* lives inside the existing, immutable
`app.agent_versions.config` JSON column; see
`voiceagent.workflows.config.WorkflowDefinition`).

**`call_workflow_executions` carries `UNIQUE(call_session_id)`** -- at most
one execution per call, the durable idempotency guard against two
concurrent `workflow.advance` tool calls both running the workflow (brief
TRANSACTIONS/CONCURRENCY). The same `CHECK`-plus-application-validation
discipline every other status/enum column in this schema already has
governs `status` and `failure_reason` here too.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from infra.db.rls import tenant_rls_statements

revision: str = "0008_call_workflow_executions"
down_revision: str | Sequence[str] | None = "0007_follow_up_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "call_workflow_executions"
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
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_config_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'running'")
        ),
        sa.Column("current_step_id", sa.String(length=100), nullable=False),
        sa.Column("steps_executed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=50), nullable=True),
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
            name="fk_call_workflow_executions_call_session",
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id", "tenant_id"],
            ["app.agent_versions.id", "app.agent_versions.tenant_id"],
            name="fk_call_workflow_executions_agent_version",
        ),
        sa.UniqueConstraint("call_session_id", name="uq_call_workflow_executions_call_session"),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name="ck_call_workflow_executions_status",
        ),
        sa.CheckConstraint(
            "steps_executed >= 0", name="ck_call_workflow_executions_steps_executed_non_negative"
        ),
        sa.CheckConstraint(
            "workflow_config_hash ~ '^[0-9a-f]{64}$'",
            name="ck_call_workflow_executions_config_hash_format",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN ('tool_step_failed', "
            "'max_steps_exceeded', 'execution_conflict', 'invalid_workflow_definition', "
            "'unexpected_error')",
            name="ck_call_workflow_executions_failure_reason",
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'failed', 'cancelled')) = (ended_at IS NOT NULL)",
            name="ck_call_workflow_executions_ended_at_iff_terminal",
        ),
        schema="app",
    )
    op.create_index("ix_call_workflow_executions_tenant_id", _TABLE, ["tenant_id"], schema="app")
    op.create_index(
        "ix_call_workflow_executions_agent_version_id",
        _TABLE,
        ["agent_version_id"],
        schema="app",
    )
    op.create_index("ix_call_workflow_executions_status", _TABLE, ["status"], schema="app")

    for statement in tenant_rls_statements(_TABLE, schema="app"):
        op.execute(statement)

    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON app.{_TABLE} TO "{app_role}"')


def downgrade() -> None:
    app_role = _app_role()
    op.execute(f'REVOKE SELECT, INSERT, UPDATE, DELETE ON app.{_TABLE} FROM "{app_role}"')
    op.drop_table(_TABLE, schema="app")
