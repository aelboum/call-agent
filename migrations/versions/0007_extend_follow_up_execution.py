"""extend app.follow_up_actions for safe scheduled execution

Revision ID: 0007_follow_up_execution
Revises: 0006_call_analysis
Create Date: 2026-09-22

Phase 2.9 -- FollowUpAction to safe scheduled execution, exactly as
specified by `docs/PHASE-2.9-STATUS.md`. `app.follow_up_actions` (`0005`)
remains the one and only authoritative table (brief §3: "Do not create a
second follow-up table") -- this migration extends it with the minimum
additional columns/constraints/index the execution lifecycle needs, the
same "a later migration adds what a new capability needs, never by editing
history" discipline `0005`'s own docstring already established for
`calendar_events`.

**Step 0/1 widen, never weaken, the existing `status`/`type` shape.**
`ck_follow_up_actions_status` is dropped and recreated with the two new
values (`'processing'`, `'failed'`) added to Phase 2.7's three -- every
previously-valid value stays valid, and RLS/FORCE/grants on the table are
untouched (brief §14: "Do not weaken existing policies").

**Step 2 adds the five execution-metadata columns** brief §8 asks for
(`attempt_count`, `next_attempt_at`, `last_attempted_at`, `completed_at`,
`failure_reason`, `execution_id` -- six, matching
`voiceagent.followups.models.FollowUpAction`'s own docstring exactly) plus
two new CHECK constraints: `attempt_count >= 0`, and a closed vocabulary for
`failure_reason` (`voiceagent.followups.retry_policy.FAILURE_REASONS`) --
brief §8's "Do NOT store sensitive exception traces" enforced at the
database layer too, not just by application code, matching this schema's
established "CHECK plus application validation, never one alone"
discipline.

**Step 3 adds `ix_follow_up_actions_claim_lookup`**, the one partial index
`claim_due_follow_up()`'s due-lookup query needs (brief §14): `(tenant_id,
next_attempt_at) WHERE next_attempt_at IS NOT NULL`. `next_attempt_at` is
non-NULL only for a follow-up `voiceagent.followups.retry_policy
.EXECUTABLE_TYPES` covers (brief §5), so the partial predicate keeps the
index small and keeps a non-executable follow-up out of it entirely.

Every existing row backfills to the new columns' defaults
(`attempt_count=0`, every new nullable column `NULL`) -- an already-`pending`
or already-terminal Phase 2.7 row is unaffected; it becomes eligible for
automatic claim only once `voiceagent.followups.service.create_follow_up()`
(a `voiceagent.followups.retry_policy.EXECUTABLE_TYPES` row created *after*
this migration) seeds `next_attempt_at` from `due_at`, exactly as new rows
do -- there is no backfill of `next_attempt_at` for pre-existing rows,
deliberately: this migration adds capability, it does not retroactively
schedule anything that was not already going to be scheduled.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_follow_up_execution"
down_revision: str | Sequence[str] | None = "0006_call_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "follow_up_actions"


def upgrade() -> None:
    # --- 0/1: widen ck_follow_up_actions_status, never weaken RLS/grants ---
    op.drop_constraint("ck_follow_up_actions_status", _TABLE, schema="app", type_="check")
    op.create_check_constraint(
        "ck_follow_up_actions_status",
        _TABLE,
        "status IN ('pending', 'processing', 'completed', 'cancelled', 'failed')",
        schema="app",
    )

    # --- 2: execution metadata columns --------------------------------------
    op.add_column(
        _TABLE,
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        schema="app",
    )
    op.add_column(
        _TABLE,
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )
    op.add_column(
        _TABLE,
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )
    op.add_column(
        _TABLE,
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )
    op.add_column(
        _TABLE,
        sa.Column("failure_reason", sa.String(length=100), nullable=True),
        schema="app",
    )
    op.add_column(
        _TABLE,
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        schema="app",
    )
    op.create_check_constraint(
        "ck_follow_up_actions_attempt_count_non_negative",
        _TABLE,
        "attempt_count >= 0",
        schema="app",
    )
    op.create_check_constraint(
        "ck_follow_up_actions_failure_reason",
        _TABLE,
        "failure_reason IS NULL OR failure_reason IN ('calendar_event_not_found', "
        "'calendar_event_cancelled', 'max_attempts_exceeded', 'unexpected_error')",
        schema="app",
    )

    # --- 3: due-lookup partial index ----------------------------------------
    op.create_index(
        "ix_follow_up_actions_claim_lookup",
        _TABLE,
        ["tenant_id", "next_attempt_at"],
        schema="app",
        postgresql_where=sa.text("next_attempt_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_follow_up_actions_claim_lookup", table_name=_TABLE, schema="app")

    op.drop_constraint("ck_follow_up_actions_failure_reason", _TABLE, schema="app", type_="check")
    op.drop_constraint(
        "ck_follow_up_actions_attempt_count_non_negative", _TABLE, schema="app", type_="check"
    )
    op.drop_column(_TABLE, "execution_id", schema="app")
    op.drop_column(_TABLE, "failure_reason", schema="app")
    op.drop_column(_TABLE, "completed_at", schema="app")
    op.drop_column(_TABLE, "last_attempted_at", schema="app")
    op.drop_column(_TABLE, "next_attempt_at", schema="app")
    op.drop_column(_TABLE, "attempt_count", schema="app")

    op.drop_constraint("ck_follow_up_actions_status", _TABLE, schema="app", type_="check")
    op.create_check_constraint(
        "ck_follow_up_actions_status",
        _TABLE,
        "status IN ('pending', 'completed', 'cancelled')",
        schema="app",
    )
