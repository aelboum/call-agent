"""create app.conversation_turns

Revision ID: 0003_conversation_turns
Revises: 0002_domain_foundation
Create Date: 2026-09-22

Phase 2.5 -- durable conversation history, exactly as specified by
`docs/PHASE-2.5-STATUS.md`. One table only -- no `conversations` wrapper
table (`voiceagent/conversations/models.py`'s own module docstring explains
why one call is always exactly one conversation, so `call_session_id` is
this table's own conversation identity, not a second FK to an intermediate
row).

**Composite tenant-aware foreign key** into `app.call_sessions`
(`(call_session_id, tenant_id) REFERENCES (call_sessions.id,
call_sessions.tenant_id)`), exactly the pattern `0002`'s own docstring
established -- a cross-tenant reference is a constraint violation, not
merely an RLS-hidden row. `ON DELETE CASCADE`: no `CallSession`-deleting
service exists anywhere in this codebase today, so this is documented,
forward-looking defense against an orphaned turn (Phase 2.5 brief section
10), not a behavior this migration's own test suite can exercise via any
current application code path.

**Ordering is a plain per-call `Integer`, not a database identity/serial
column** -- computed in application code
(`voiceagent.conversations.service.persist_conversation_turn()`) under a
`SELECT ... FOR UPDATE` lock on the owning `CallSession` row, matching this
product's existing "no `sqlalchemy.func`" discipline (ADR-0007) and the
`claim_runtime_ownership()` locking pattern `0002`'s own `call_sessions`
table already relies on.

**Idempotency** is `UNIQUE(call_session_id, event_id, role)` -- `role` is
part of the key because a tool call's own "tool_call" and "tool_result"
turns share one `event_id` (`ToolCallRequested.call_id`/
`ToolResult.call_id`); without `role`, persisting the second would be
indistinguishable from a duplicate delivery of the first and would be
silently dropped (caught against a real database during this migration's
own verification, not merely reasoned about). A retried persistence
attempt for the same `(event_id, role)` is rejected by this constraint at
the database layer even if the application-level idempotency check (under
the same row lock) were somehow bypassed -- defense in depth, not the only
enforcement.

**Step 0 fixes a real gap in `0002`**: that migration gave `agents`,
`agent_versions` and `phone_numbers` each a `UNIQUE(id, tenant_id)` (the
mechanism a composite tenant-aware FK requires of the table it points at),
but never gave one to `call_sessions` -- because nothing referenced
`call_sessions` as a parent until now. Verified against a real PostgreSQL
16 instance: `CREATE TABLE app.conversation_turns (...)` fails with
`there is no unique constraint matching given keys for referenced table
"call_sessions"` without it. `0002` itself is already-applied, committed
history (Phase 2.1) and is not edited retroactively; this migration adds
the missing constraint as its own first step instead, exactly the same
"a later migration fixes an earlier gap" pattern any schema history uses.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from infra.db.rls import tenant_rls_statements

revision: str = "0003_conversation_turns"
down_revision: str | Sequence[str] | None = "0002_domain_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_APP_ROLE = "saas_os_app"

_PAYLOAD_SHAPE_CHECK = (
    "(role IN ('system', 'user', 'assistant') AND content IS NOT NULL "
    "AND tool_payload IS NULL) OR "
    "(role IN ('tool_call', 'tool_result') AND tool_payload IS NOT NULL)"
)


def _app_role() -> str:
    role = os.environ.get("APP_DB_USER", _DEFAULT_APP_ROLE)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
        raise ValueError(f"APP_DB_USER must be a plain SQL identifier, got: {role!r}")
    return role


def upgrade() -> None:
    app_role = _app_role()
    now = sa.func.now()

    # --- 0: the missing uq_call_sessions_id_tenant, see module docstring ---
    op.create_unique_constraint(
        "uq_call_sessions_id_tenant", "call_sessions", ["id", "tenant_id"], schema="app"
    )

    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("core.tenants.id"), nullable=False),
        sa.Column("call_session_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_payload", sa.JSON(), nullable=True),
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
            name="fk_conversation_turns_call_session",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "call_session_id",
            "event_id",
            "role",
            name="uq_conversation_turns_call_event_role",
        ),
        sa.UniqueConstraint(
            "call_session_id", "sequence", name="uq_conversation_turns_call_sequence"
        ),
        sa.CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool_call', 'tool_result')",
            name="ck_conversation_turns_role",
        ),
        sa.CheckConstraint(_PAYLOAD_SHAPE_CHECK, name="ck_conversation_turns_payload_shape"),
        schema="app",
    )
    op.create_index(
        "ix_conversation_turns_tenant_id", "conversation_turns", ["tenant_id"], schema="app"
    )
    op.create_index(
        "ix_conversation_turns_call_session_sequence",
        "conversation_turns",
        ["call_session_id", "sequence"],
        schema="app",
    )

    for statement in tenant_rls_statements("conversation_turns", schema="app"):
        op.execute(statement)

    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON app.conversation_turns TO "{app_role}"')


def downgrade() -> None:
    app_role = _app_role()
    op.execute(f'REVOKE SELECT, INSERT, UPDATE, DELETE ON app.conversation_turns FROM "{app_role}"')
    op.drop_table("conversation_turns", schema="app")
    op.drop_constraint("uq_call_sessions_id_tenant", "call_sessions", schema="app", type_="unique")
