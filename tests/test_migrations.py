"""Migration configuration sanity (Phase 1 brief sections 3, 4, 14).

These run Alembic in *offline* mode: it renders SQL without connecting to
anything, so the migration's actual output can be asserted in a hermetic
suite. That is worth more than a smoke test of the config file -- it proves
the migration compiles, targets the `app` schema, and issues the grants the
application role needs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_MIGRATION_ENVIRONMENT = {
    # A migration runs as the schema-owning role, which is deliberately not
    # the role the application runs as: the platform refuses to serve traffic
    # under a superuser/BYPASSRLS role, so the two must differ.
    "MIGRATIONS_DATABASE_URL": "postgresql+psycopg://owner:unused@127.0.0.1:1/unused",
    "APP_DB_USER": "saas_os_app",
}


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, no user input
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **_MIGRATION_ENVIRONMENT},
        check=False,
    )


@pytest.fixture(scope="module")
def offline_sql() -> str:
    result = _alembic("upgrade", "head", "--sql")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_there_is_exactly_one_head() -> None:
    """A branched history is an ambiguity that only shows up on deploy."""
    result = _alembic("heads")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("(head)") == 1


def test_migration_creates_the_product_schema(offline_sql: str) -> None:
    assert "CREATE SCHEMA IF NOT EXISTS app" in offline_sql


def test_migration_grants_to_the_application_role(offline_sql: str) -> None:
    """The product owns the grants inside its own schema; the platform does
    not issue them, and running the application as a superuser to avoid them
    would be refused at startup anyway."""
    assert 'GRANT USAGE ON SCHEMA app TO "saas_os_app"' in offline_sql
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA app" in offline_sql


def test_migration_never_touches_a_platform_schema(offline_sql: str) -> None:
    """`core` is SaaS-OS's; this history creates, alters and drops nothing in
    it. Referencing `core.tenants` by foreign key (a later migration) is a
    read-shaped dependency, not a modification."""
    forbidden = ("CREATE SCHEMA IF NOT EXISTS core", "DROP SCHEMA", "ALTER TABLE core.")
    for statement in forbidden:
        assert statement not in offline_sql


def test_version_table_is_distinct_from_the_platform_history(offline_sql: str) -> None:
    """SaaS-OS ADR-0016: two independent histories in one database. The
    platform tracks `alembic_version_saas_os`; this one tracks the default
    `alembic_version`. Sharing a table would make each history's migrations
    look applied to the other."""
    assert "INSERT INTO alembic_version " in offline_sql
    assert "alembic_version_saas_os" not in offline_sql


def test_deferred_domain_tables_are_never_created(offline_sql: str) -> None:
    """Superseded 2026-09-21 (Phase 2.1), 2026-09-22 (Phase 2.5), and again
    2026-09-22 (Phase 2.6): the Phase 1 version of this test asserted no
    migration ever created a table, because Phase 1 had no domain. Phase 2.1
    IS the first domain slice (`app.agents`, `app.agent_versions`,
    `app.phone_numbers`, `app.call_sessions`); Phase 2.5 adds durable
    conversation history (`app.conversation_turns`); Phase 2.6 adds Contacts
    and a minimal internal calendar (`app.contacts`, `app.calendars`,
    `app.calendar_events`) -- all correctly absent from this list now,
    rather than a regression. `conversations` stays deferred permanently,
    not merely "not yet": `voiceagent/conversations/models.py`'s own module
    docstring records why this product has no use for a conversation
    identity independent of `call_session_id`. `appointments` also stays
    deferred permanently, not "not yet": Phase 2.6's appointment table is
    named `calendar_events` (`docs/PHASE-2.6-STATUS.md`), not `appointments`.
    Every other Phase 2.0-deferred table (Phase 2.0 report §11.2 / Phase 2.1
    brief §3) must still never appear, in this migration or any other, ahead
    of the phase that actually needs it."""
    deferred_tables = (
        "conversations",
        "contact_phones",
        "working_hours",
        "appointments",
        "tools",
        "tool_bindings",
        "workflows",
        "workflow_versions",
        "recordings",
        "provider_credentials",
        "runtime_assignments",
    )
    for table in deferred_tables:
        assert f"CREATE TABLE app.{table} (" not in offline_sql


def test_conversation_turns_table_is_created(offline_sql: str) -> None:
    assert "CREATE TABLE app.conversation_turns (" in offline_sql


def test_conversation_turns_has_row_level_security(offline_sql: str) -> None:
    assert 'ALTER TABLE "app"."conversation_turns" ENABLE ROW LEVEL SECURITY' in offline_sql
    assert 'ALTER TABLE "app"."conversation_turns" FORCE ROW LEVEL SECURITY' in offline_sql


def test_phase_2_6_tables_are_created(offline_sql: str) -> None:
    assert "CREATE TABLE app.contacts (" in offline_sql
    assert "CREATE TABLE app.calendars (" in offline_sql
    assert "CREATE TABLE app.calendar_events (" in offline_sql


def test_phase_2_6_tables_have_row_level_security(offline_sql: str) -> None:
    for table in ("contacts", "calendars", "calendar_events"):
        assert f'ALTER TABLE "app"."{table}" ENABLE ROW LEVEL SECURITY' in offline_sql
        assert f'ALTER TABLE "app"."{table}" FORCE ROW LEVEL SECURITY' in offline_sql


def test_call_sessions_contact_id_column_is_added(offline_sql: str) -> None:
    assert "ADD COLUMN contact_id" in offline_sql
    assert "fk_call_sessions_contact" in offline_sql
