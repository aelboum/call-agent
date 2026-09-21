"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Conventions every product migration follows (ADR-0007, ADR-0002 of SaaS-OS):

* Tables are created in the `app` schema. Nothing in this history ever
  modifies a SaaS-OS-owned schema.
* Every tenant-owned table carries
  `tenant_id uuid NOT NULL REFERENCES core.tenants(id)`, is indexed on it, and
  applies `infra.db.rls.tenant_rls_statements(table, schema="app")` -- which
  emits `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY` and the
  tenant-isolation policy. Never weaken, skip, or hand-roll those.
* Grants to the application role are this history's responsibility, not the
  platform's.
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401 -- available to every migration by convention
from alembic import op  # noqa: F401

revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
