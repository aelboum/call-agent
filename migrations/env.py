"""The product's Alembic environment (SaaS-OS ADR-0016, product ADR-0007).

Independent of the platform's own environment: its own script directory, its
own default-named `alembic_version` table, and invoked separately, after the
platform's migrations.

**`target_metadata` is `None`, and must stay `None`.** Product models are
declared on `infra.db.Base`, whose `MetaData` is shared with every SaaS-OS
table (core modules declare on the same base). Pointing autogenerate at it
would let Alembic compare the platform's own schema against this product's
model set and propose dropping or recreating tables this history does not own.
Migrations here are hand-written; `create_all()` is never called, in any
environment, including tests.

If autogenerate is ever wanted it requires `include_schemas=True` plus an
`include_object` filter restricted to the `app` schema, introduced by its own
ADR -- not by editing this line.

Importing SQLAlchemy directly here is correct and sanctioned: migration code
owns physical PostgreSQL types (ADR-0007), and a migration emits DDL in its
own transaction -- it never runs inside a tenant-scoped session and so cannot
bypass a tenant policy. Application code, which can, may not (ADR-0007).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from infra.db.config import get_migrations_database_config
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = None


def _get_url() -> str:
    return get_migrations_database_config().url


def run_migrations_offline() -> None:
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _get_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
