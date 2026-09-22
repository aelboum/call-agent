"""create app.call_analysis

Revision ID: 0006_call_analysis
Revises: 0005_call_outcomes_followups
Create Date: 2026-09-22

Phase 2.8 -- a small, deterministic, derived post-call analysis foundation,
exactly as specified by `docs/PHASE-2.8-STATUS.md`. One table only -- no
workflow table, no event-bus table, no generic analytics table (brief
§13).

**`call_analysis` is never a second source of truth** (brief §4): every
value it carries is a rebuildable snapshot computed from already-persisted
`call_sessions`/`conversation_turns`/`call_outcomes`/`follow_up_actions`
rows by `voiceagent.call_analysis.service.build_call_analysis()`; this
migration adds no new authority, only a cache-shaped table with the usual
tenant isolation.

**`UNIQUE(call_session_id)`** -- one analysis per call (brief §3/§9),
exactly the same mechanism `0005`'s own `uq_call_outcomes_call_session`
already established for `call_outcomes`.

**`CHECK status IN ('pending', 'ready')`** -- brief §9: "database
constraints for status/enums where the existing project convention
supports them", the same discipline every other status/type/outcome column
in this schema already has.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from infra.db.rls import tenant_rls_statements

revision: str = "0006_call_analysis"
down_revision: str | Sequence[str] | None = "0005_call_outcomes_followups"
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
    now = sa.func.now()

    op.create_table(
        "call_analysis",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("call_session_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'ready'")
        ),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("user_turn_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "assistant_turn_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tool_result_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("had_transfer", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("had_hold", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "contact_associated", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("outcome", sa.String(length=30), nullable=True),
        sa.Column("follow_up_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "appointment_follow_up_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "open_follow_up_count", sa.Integer(), nullable=False, server_default=sa.text("0")
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
            ["call_session_id", "tenant_id"],
            ["app.call_sessions.id", "app.call_sessions.tenant_id"],
            name="fk_call_analysis_call_session",
        ),
        sa.UniqueConstraint("call_session_id", name="uq_call_analysis_call_session"),
        sa.CheckConstraint("status IN ('pending', 'ready')", name="ck_call_analysis_status"),
        sa.CheckConstraint("turn_count >= 0", name="ck_call_analysis_turn_count_non_negative"),
        schema="app",
    )
    op.create_index("ix_call_analysis_tenant_id", "call_analysis", ["tenant_id"], schema="app")

    for statement in tenant_rls_statements("call_analysis", schema="app"):
        op.execute(statement)

    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON app.call_analysis TO "{app_role}"')


def downgrade() -> None:
    app_role = _app_role()
    op.execute(f'REVOKE SELECT, INSERT, UPDATE, DELETE ON app.call_analysis FROM "{app_role}"')
    op.drop_table("call_analysis", schema="app")
