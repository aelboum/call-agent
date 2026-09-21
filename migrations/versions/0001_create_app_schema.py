"""create the product's `app` schema

Revision ID: 0001_app_schema
Revises:
Create Date: 2026-09-21

The foundation migration. It creates the product's own schema and the
privileges the application role needs inside it -- and deliberately creates no
table: Phase 1 has no domain, and a table invented ahead of its domain is a
migration that will have to be rewritten.

What it establishes for every later migration:

* The product owns `app` and only `app`. SaaS-OS's schemas (`core`, and its
  own migration history) are never touched from here.
* `ALTER DEFAULT PRIVILEGES` means a future table created by this history is
  usable by the application role without repeating a grant -- and, just as
  importantly, without anyone being tempted to run the application as a
  superuser to avoid the grant. `api.platform.build_platform_app()` refuses to
  start against a superuser/`BYPASSRLS` role, so that shortcut would fail
  closed anyway.
* The application role name comes from `APP_DB_USER` and is validated as a
  plain SQL identifier before interpolation, because a role name cannot be
  passed as a bind parameter in DDL.

Row-Level Security is not applied here because there is no table to apply it
to. `tests/architecture/test_rls_integration.py` verifies the platform's
`tenant_rls_statements()` helper -- the path every future table will use --
emits `ENABLE`, `FORCE` and a `current_setting('app.tenant_id')`-keyed policy,
so the contract is pinned by a test before the first table exists.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import op

revision: str = "0001_app_schema"
down_revision: str | Sequence[str] | None = None
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

    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute(f'GRANT USAGE ON SCHEMA app TO "{app_role}"')
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app "
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{app_role}"'
    )


def downgrade() -> None:
    app_role = _app_role()

    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app "
        f'REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM "{app_role}"'
    )
    # RESTRICT, never CASCADE: if a later migration left a table behind, the
    # downgrade must fail loudly rather than silently dropping product data.
    op.execute("DROP SCHEMA IF EXISTS app RESTRICT")
