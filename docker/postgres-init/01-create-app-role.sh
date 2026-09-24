#!/bin/sh
# Runs automatically, once, only when the `postgres` container initializes an
# empty data directory (the official postgres image's own
# docker-entrypoint-initdb.d convention -- shell scripts placed there run
# with this database's own environment already available, and are skipped on
# every later restart against an existing volume). This is the exact
# statement `tests/integration/README.md` already documents an operator
# running by hand; automating it here does not change what it does, only who
# types it -- see `docs/PHASE-2.18-DEPLOYMENT-FOUNDATION.md`.
#
# Connecting as the schema-owning role (`$POSTGRES_USER`, this compose
# stack's superuser) is not optional for the *application* at runtime: a
# superuser/BYPASSRLS role bypasses Row-Level Security entirely, and the
# platform's own startup guard refuses to serve traffic under one
# (`docs/PHASE-2.16-SECURITY-READINESS.md`). `saas_os_app` is the restricted
# role the API and every worker process connect as instead.
#
# `POSTGRES_APP_PASSWORD` is `docker-compose.yml`'s own local/staging
# default -- never a real production credential.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE ROLE saas_os_app LOGIN PASSWORD '$POSTGRES_APP_PASSWORD'
        NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
    GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO saas_os_app;
SQL
