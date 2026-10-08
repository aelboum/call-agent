"""Phase 2.41: real-PostgreSQL verification of the product's own Alembic
migration chain (`migrations/`) for safe rollback and re-upgrade.

The Phase 2.36 production-readiness audit identified forward migrations as
validated but migration rollback as "documented but NOT validated at
production scale" -- Phase 2.37 then validated backup/restore but
deliberately left this gap open (`docs/PHASE-2.37-BACKUP-RECOVERY-FOUNDATION.md`).
This module closes it the same way Phase 2.1 closed the RLS gap
(`tests/integration/test_domain_rls_integration.py`): a real disposable
PostgreSQL instance, not a mock, because the property under test --
whether a hand-written `downgrade()` actually reconstructs the prior
schema, and whether it can do so with production-shaped data already in
the table -- cannot be observed any other way.

Requires a real PostgreSQL instance with SaaS-OS's own migrations and this
product's migrations already applied (`tests/integration/README.md`) --
excluded from the default `pytest` run (`pytest -m integration` to run it
explicitly). Tests in this module run in *file order* (no randomization is
configured for this suite -- see `pyproject.toml`) and are intentionally
schema-destructive: `test_full_downgrade_to_base_and_back_restores_clean_schema`
drops the product's entire `app` schema down to `base` and rebuilds it.
This is safe only because `tests/ops` is collected after `tests/integration`
(directory traversal order), so no other test's data is depended upon
afterward, and because a disposable PostgreSQL instance is, by this
project's own convention (`tests/integration/README.md` "no permanent
local/CI harness"), never a database anything else needs to still contain
data after the run. Do not add a test after this module's full-rollback
test that assumes pre-existing tenant data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from core.identity import create_user
from core.tenancy import create_tenant

from voiceagent.agents.config import AgentConfig
from voiceagent.agents.models import Agent
from voiceagent.agents.service import create_agent, create_draft_version, publish_version
from voiceagent.calendars.service import create_calendar
from voiceagent.calls.service import create_call_session
from voiceagent.db import tenant_session_scope
from voiceagent.followups.service import (
    claim_due_follow_up,
    create_follow_up,
    fail_follow_up_execution,
)
from voiceagent.phone_numbers.service import register_phone_number
from voiceagent.tenancy import TenantContext

pytestmark = pytest.mark.integration

# The expected chain, pinned so a future migration that silently rewrites
# history (rather than appending to it) fails this test loudly. Appending a
# 0013_* migration is expected to require updating this tuple -- that is
# the point: the chain's shape is a deliberate fact, not an accident of
# whatever happens to be on disk.
_EXPECTED_CHAIN = (
    "0001_app_schema",
    "0002_domain_foundation",
    "0003_conversation_turns",
    "0004_contacts_calendar",
    "0005_call_outcomes_followups",
    "0006_call_analysis",
    "0007_follow_up_execution",
    "0008_call_workflow_executions",
    "0009_knowledge_tables",
    "0010_call_ai_analyses",
    "0011_call_sessions_index",
    "0012_inbound_routing",
)


def _alembic_config() -> Config:
    return Config("alembic.ini")


def _chain(cfg: Config) -> list[str]:
    script = ScriptDirectory.from_config(cfg)
    revisions = list(script.walk_revisions("base", "heads"))
    revisions.reverse()
    return [r.revision for r in revisions]


def _db_revision(engine: sa.Engine) -> str | None:
    with engine.connect() as conn:
        if (
            conn.execute(
                sa.text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            ).scalar()
            is not True
        ):
            return None
        row = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchone()
        return row[0] if row else None


def _engine() -> sa.Engine:
    # Mirrors migrations/env.py's own `_get_url()` -- the privileged
    # bootstrap/migration role, never the restricted application role,
    # since this module issues DDL exactly as Alembic itself does.
    from infra.db.config import get_migrations_database_config

    return sa.create_engine(get_migrations_database_config().url)


def _table_inventory(engine: sa.Engine) -> dict[str, dict]:
    """A minimal, deterministic structural fingerprint of the `app` schema:
    table names, column (name, type, nullability) tuples, and constraint
    definitions -- enough to prove a downgrade reconstructed the exact
    prior shape without pulling in a second, heavier schema-diffing
    dependency."""
    with engine.connect() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'app' ORDER BY table_name"
                )
            ).fetchall()
        ]
        inventory: dict[str, dict] = {}
        for table in tables:
            columns = [
                tuple(r)
                for r in conn.execute(
                    sa.text(
                        "SELECT column_name, data_type, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'app' AND table_name = :t ORDER BY column_name"
                    ),
                    {"t": table},
                ).fetchall()
            ]
            constraints = [
                r[0]
                for r in conn.execute(
                    sa.text(
                        """
                        SELECT pg_get_constraintdef(c.oid)
                        FROM pg_constraint c
                        JOIN pg_class r ON r.oid = c.conrelid
                        JOIN pg_namespace n ON n.oid = r.relnamespace
                        WHERE n.nspname = 'app' AND r.relname = :t
                        ORDER BY c.conname
                        """
                    ),
                    {"t": table},
                ).fetchall()
            ]
            inventory[table] = {"columns": columns, "constraints": constraints}
        return inventory


def test_migration_chain_is_the_expected_linear_single_head_graph() -> None:
    """Pins the chain's topology: no unexpected branch, no missing/extra
    revision. A multi-head graph would silently change Scenario B/C's own
    walk below from "every boundary" to "every boundary on one arbitrarily
    chosen branch" -- this test exists so that change is never silent."""
    cfg = _alembic_config()
    chain = _chain(cfg)
    assert chain == list(_EXPECTED_CHAIN)

    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert heads == [_EXPECTED_CHAIN[-1]]


def test_full_downgrade_to_base_and_back_restores_clean_schema() -> None:
    """Scenario B+C combined: walk head -> base one revision at a time
    (every individual downgrade boundary, not a blunt `downgrade base`),
    record the resulting bookkeeping state at `base`, then walk base ->
    head one revision at a time again, asserting the final structural
    inventory is byte-for-byte identical to the one recorded before this
    test touched anything. Requires the database to start at head.

    This walk is run with whatever `follow_up_actions` data already exists
    in the shared disposable database at this point in the session
    (including rows other test files, e.g.
    `tests/integration/test_follow_up_execution_integration.py`, may have
    left in `'processing'`/`'failed'`) -- deliberately not cleaned up
    first, because `0007_follow_up_execution`'s own `downgrade()` now
    normalizes those rows itself (Phase 2.41 remediation; see
    `docs/PHASE-2.41-DB-MIGRATION-ROLLBACK-VALIDATION.md` SS7). This test
    passing with real, un-sanitized leftover data in that shape is itself
    part of the proof that the remediation works end-to-end, not just in
    the dedicated probe test below."""
    cfg = _alembic_config()
    engine = _engine()
    chain = _chain(cfg)

    starting_rev = _db_revision(engine)
    assert starting_rev == chain[-1], (
        f"expected to start at head {chain[-1]!r}, database is at {starting_rev!r} -- "
        "a prior test in this module must have left the schema at a non-head revision"
    )
    baseline_inventory = _table_inventory(engine)

    for rev in reversed(chain):
        before = _db_revision(engine)
        assert before == rev, f"expected {rev!r} before downgrading, database has {before!r}"
        command.downgrade(cfg, "-1")
        after = _db_revision(engine)
        expected_parent = chain[chain.index(rev) - 1] if chain.index(rev) > 0 else None
        assert after == expected_parent, (
            f"downgrading {rev!r} landed on {after!r}, expected {expected_parent!r}"
        )

    assert _db_revision(engine) is None, "no application revision should remain at base"
    with engine.connect() as conn:
        assert (
            conn.execute(sa.text("SELECT to_regnamespace('app') IS NOT NULL")).scalar() is False
        ), "the `app` schema itself must not survive a full downgrade to base"

    for rev in chain:
        before = _db_revision(engine)
        expected_before = chain[chain.index(rev) - 1] if chain.index(rev) > 0 else None
        assert before == expected_before
        command.upgrade(cfg, "+1")
        after = _db_revision(engine)
        assert after == rev, f"upgrading to {rev!r} landed on {after!r}"

    final_inventory = _table_inventory(engine)
    assert final_inventory == baseline_inventory, (
        "the schema rebuilt from base does not structurally match the schema recorded "
        "before the downgrade -- a downgrade()/upgrade() pair in this chain is not a "
        "faithful inverse"
    )


def _config_payload() -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "instructions": "Answer the phone.",
            "language": "en",
            "voice": {"provider": "fake", "voice_id": "v1"},
            "engine": {"kind": "pipelined", "stt": {"provider": "fake", "config": {}}},
            "business_hours": {"timezone": "UTC", "windows": []},
            "privacy": {"data_classification": "tenant_data", "purpose": "call_assistance"},
        }
    )


def _tenant_with_claimable_follow_up(label: str) -> tuple[TenantContext, uuid.UUID, uuid.UUID]:
    """One tenant with a published agent and one `appointment` follow-up
    already claimable (`due_at` in the past). Returns `(context, agent_id,
    follow_up_id)`."""
    tenant = create_tenant(f"phase241-{label}-{uuid.uuid4().hex[:8]}")
    user = create_user()
    context = TenantContext(tenant_id=tenant.id, actor_id=user.id, membership_id=uuid.uuid4())

    agent = create_agent(context, name=f"Probe Agent {label}")
    draft = create_draft_version(context, agent.id, config=_config_payload())
    version = publish_version(context, agent.id, draft.id)
    number = register_phone_number(context, e164=f"+1555{uuid.uuid4().int % 10**7:07d}")
    call = create_call_session(
        context,
        direction="inbound",
        from_e164="+15550100",
        to_e164=number.e164,
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
    )
    calendar = create_calendar(context, name=f"Probe Calendar {label}", timezone="UTC")
    start_at = datetime.now(UTC) + timedelta(days=1)
    follow_up = create_follow_up(
        context,
        call.id,
        type="appointment",
        calendar_id=calendar.id,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=30),
        due_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    return context, agent.id, follow_up.id


def _constraint_def(engine: sa.Engine, table: str, name: str) -> tuple[str, bool] | None:
    """`(definition, convalidated)` for one named constraint on `app.<table>`,
    or `None` if it does not exist. `convalidated` confirms PostgreSQL
    actually validated the constraint against every existing row (true for
    any `ADD CONSTRAINT` that was not given `NOT VALID` -- this migration
    never uses `NOT VALID`), not merely that the catalog entry exists."""
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                """
                SELECT pg_get_constraintdef(c.oid), c.convalidated
                FROM pg_constraint c
                JOIN pg_class r ON r.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = r.relnamespace
                WHERE n.nspname = 'app' AND r.relname = :t AND c.conname = :c
                """
            ),
            {"t": table, "c": name},
        ).fetchone()
    return (row[0], row[1]) if row else None


def test_follow_up_action_status_downgrade_normalizes_processing_and_failed_rows() -> None:
    """Phase 2.41 remediation proof: `migrations/versions/0007_extend_follow_up_execution.py`'s
    `downgrade()` now normalizes any `'processing'`/`'failed'`
    `follow_up_actions` row to `'pending'` immediately before recreating
    the narrower historical `ck_follow_up_actions_status` constraint --
    see `docs/PHASE-2.41-DB-MIGRATION-ROLLBACK-VALIDATION.md` SS7 for why
    `'pending'` (not `'completed'`/`'cancelled'`) is the historically
    correct target: neither new value is terminal (`'processing'` is still
    in flight; a non-exhausted `'failed'` row still has a future
    `next_attempt_at` and is expected to be claimed again), exactly what
    `'pending'` meant before this migration introduced the distinction.

    Proves, with two real rows reached through the fully-supported
    application path (one `'processing'` via `claim_due_follow_up()`, one
    `'failed'` via `fail_follow_up_execution()` -- both new values this
    migration itself introduced): the downgrade through `0007` now
    *succeeds* (not merely "no longer crashes" -- the rows are asserted
    normalized and the recreated constraint is asserted both present and
    actually validated), the chain can upgrade back through `0007` to
    head again, and RLS/tenant isolation is unaffected throughout."""
    cfg = _alembic_config()
    engine = _engine()
    assert _db_revision(engine) == _EXPECTED_CHAIN[-1], "must start at head"

    context_processing, agent_id_processing, follow_up_id_processing = (
        _tenant_with_claimable_follow_up("processing")
    )
    claimed_processing = claim_due_follow_up(context_processing, now=datetime.now(UTC))
    assert claimed_processing is not None and claimed_processing.id == follow_up_id_processing
    assert claimed_processing.status == "processing"  # left in-flight, deliberately not failed

    context_failed, agent_id_failed, follow_up_id_failed = _tenant_with_claimable_follow_up(
        "failed"
    )
    claimed_failed = claim_due_follow_up(context_failed, now=datetime.now(UTC))
    assert claimed_failed is not None and claimed_failed.id == follow_up_id_failed
    assert claimed_failed.execution_id is not None
    failed = fail_follow_up_execution(
        context_failed,
        follow_up_id_failed,
        execution_id=claimed_failed.execution_id,
        reason="calendar_event_not_found",
    )
    assert failed.status == "failed"

    # 1/2: both rows exist, in 'processing' and 'failed' respectively,
    # confirmed directly against the database (not just the return value).
    with engine.connect() as conn:
        statuses_before = {
            row[0]: row[1]
            for row in conn.execute(
                sa.text("SELECT id, status FROM app.follow_up_actions WHERE id IN (:p, :f)"),
                {"p": str(follow_up_id_processing), "f": str(follow_up_id_failed)},
            ).fetchall()
        }
    assert statuses_before == {
        follow_up_id_processing: "processing",
        follow_up_id_failed: "failed",
    }

    for rev in reversed(_EXPECTED_CHAIN[_EXPECTED_CHAIN.index("0007_follow_up_execution") + 1 :]):
        before = _db_revision(engine)
        assert before == rev
        command.downgrade(cfg, "-1")

    assert _db_revision(engine) == "0007_follow_up_execution"

    # 3: downgrade through 0007 now succeeds -- no IntegrityError.
    command.downgrade(cfg, "-1")
    assert _db_revision(engine) == "0006_call_analysis"

    # 4: both rows normalized to 'pending', not merely "did not crash".
    with engine.connect() as conn:
        statuses_after = {
            row[0]: row[1]
            for row in conn.execute(
                sa.text("SELECT id, status FROM app.follow_up_actions WHERE id IN (:p, :f)"),
                {"p": str(follow_up_id_processing), "f": str(follow_up_id_failed)},
            ).fetchall()
        }
    assert statuses_after == {
        follow_up_id_processing: "pending",
        follow_up_id_failed: "pending",
    }

    # 5: the historical constraint exists, is the narrow three-value
    # definition, and was actually validated (not added NOT VALID).
    constraint = _constraint_def(engine, "follow_up_actions", "ck_follow_up_actions_status")
    assert constraint is not None
    definition, convalidated = constraint
    assert "'pending'" in definition and "'completed'" in definition and "'cancelled'" in definition
    assert "'processing'" not in definition and "'failed'" not in definition
    assert convalidated is True

    # 6: upgrade back through 0007 to head succeeds again.
    command.upgrade(cfg, "head")
    assert _db_revision(engine) == _EXPECTED_CHAIN[-1]

    # 7: resulting schema is structurally correct -- the widened
    # constraint and execution-metadata columns are back, and both rows
    # (now permanently 'pending': upgrading never retroactively restores
    # the in-flight status a downgrade already normalized away, the same
    # "the schema is rebuilt, the data is not" rule every other
    # destructive boundary in this chain already follows) still exist.
    widened = _constraint_def(engine, "follow_up_actions", "ck_follow_up_actions_status")
    assert widened is not None
    assert "'processing'" in widened[0] and "'failed'" in widened[0]
    with engine.connect() as conn:
        final_statuses = {
            row[0]: row[1]
            for row in conn.execute(
                sa.text("SELECT id, status FROM app.follow_up_actions WHERE id IN (:p, :f)"),
                {"p": str(follow_up_id_processing), "f": str(follow_up_id_failed)},
            ).fetchall()
        }
    assert final_statuses == {
        follow_up_id_processing: "pending",
        follow_up_id_failed: "pending",
    }

    # 8: RLS/tenant isolation remains intact after the round-trip -- the
    # 'processing' tenant can never see the 'failed' tenant's agent and
    # vice versa.
    with tenant_session_scope(context_processing.tenant_id) as session:
        assert session.get(Agent, agent_id_failed) is None
    with tenant_session_scope(context_failed.tenant_id) as session:
        assert session.get(Agent, agent_id_processing) is None
