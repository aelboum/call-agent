"""add a composite (tenant_id, status) index to app.call_sessions

Revision ID: 0011_call_sessions_index
Revises: 0010_call_ai_analyses
Create Date: 2026-09-23

Phase 2.16 production/security readiness audit finding: `app.call_sessions`
had only single-column indexes on `tenant_id` (`ix_call_sessions_tenant_id`)
and `status` (`ix_call_sessions_status`) separately, never a composite
covering both -- despite being the table behind this product's
highest-pressure recurring query pattern:
`voiceagent.calls.service.list_non_terminal_call_sessions()` (`WHERE
tenant_id = ? AND status NOT IN (terminal...)`), called once per tenant per
scan by *both* `voiceagent.runtime.reconciliation.reconcile_tenant()` and
`voiceagent.runtime.stuck_calls.detect_stuck_calls_for_tenant()` -- and
`list_call_sessions(status=...)`, the API-facing equivalent
(`GET /v1/call-sessions?status=...`).

This is purely additive and backward-compatible: adding an index changes no
existing query's *result*, only its plan, and this migration adds nothing
else (no column, no constraint, no data change) -- every existing row and
every previously-valid query keeps working unchanged. `follow_up_actions`
and `call_ai_analyses` already received purpose-built composite indexes for
their own equivalent claim-lookup queries (`0007`, `0010`); this migration
gives `call_sessions` the same treatment for its own highest-traffic
pattern, one already-established precedent, not a new one.

**Rollout consideration** (documented per this phase's own brief, not
solved here): `CREATE INDEX` (without `CONCURRENTLY`) takes a brief
`SHARE`-level lock that blocks concurrent writes to `call_sessions` for the
duration of the index build. On a small-to-moderate table this is
negligible; on a very large production table, an operator should consider
running the equivalent `CREATE INDEX CONCURRENTLY` by hand outside this
migration's transaction instead (Alembic's default offline/online mode runs
every migration inside one transaction, which `CONCURRENTLY` cannot
participate in) -- this migration does not attempt that, matching every
other index this product's migrations already create the same plain way
(`0005`, `0007`, `0009`, `0010`), so introducing `CONCURRENTLY` here alone
would be an inconsistent, one-off deviation rather than a real fix.

**Phase 2.17 release-validation fix**: this migration's `revision` id was
originally `"0011_call_sessions_tenant_status_index"` (38 chars) -- longer
than Alembic's own `alembic_version.version_num VARCHAR(32)` column (every
other migration's id in this repository is 29 chars or fewer). Running
`alembic upgrade head` against a real PostgreSQL instance failed with
`StringDataRightTruncation` on the version-table `UPDATE` -- a real defect
this migration had never actually been applied against a database before,
only rendered offline as SQL text (`tests/test_migrations.py`'s own
hermetic suite never executes DDL, so it could not have caught this).
Shortened to `"0011_call_sessions_index"` (25 chars); the index name/shape
and every other statement are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_call_sessions_index"
down_revision: str | Sequence[str] | None = "0010_call_ai_analyses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "call_sessions"
_INDEX = "ix_call_sessions_tenant_status"


def upgrade() -> None:
    op.create_index(_INDEX, _TABLE, ["tenant_id", "status"], schema="app")


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE, schema="app")
