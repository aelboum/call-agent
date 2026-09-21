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
    """Superseded 2026-09-21 (Phase 2.1): the Phase 1 version of this test
    asserted no migration ever created a table, because Phase 1 had no
    domain. Phase 2.1 IS the first domain slice (`app.agents`,
    `app.agent_versions`, `app.phone_numbers`, `app.call_sessions`), so that
    assertion is now correctly false rather than a regression -- it is
    replaced by its own descendant: the Phase 2.0-deferred tables (Phase 2.0
    report §11.2 / Phase 2.1 brief §3) must never appear, in this migration
    or any other, ahead of the phase that actually needs them."""
    deferred_tables = (
        "conversations",
        "conversation_turns",
        "contacts",
        "contact_phones",
        "calendars",
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
