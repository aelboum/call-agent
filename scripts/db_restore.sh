#!/usr/bin/env bash
# Phase 2.37: minimal, deterministic PostgreSQL restore, paired with
# db_backup.sh. Restores a pg_dump custom-format archive into a target
# database that must already exist (an operator creates it, e.g. via the
# same role-provisioning step `docker/postgres-init/01-create-app-role.sh`
# runs for a fresh deployment) and is expected to be EMPTY of this
# product's own schema before restoring.
#
# Fails closed on a missing, empty, or structurally-corrupt input archive --
# never silently reports success against a bad artifact.
#
# WARNING: running this against a database that already holds real data
# is destructive to whatever is currently there once objects are restored
# on top of it. This script does not protect against that -- it is an
# operator's job to point --db at an intentionally fresh target. Restoring
# over a live production database is not a supported use of this script;
# it requires an explicit, separate, reviewed operational procedure.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: db_restore.sh --host H --port P --db NAME --user USER --input FILE

Environment:
  PGPASSWORD   required; password for --user (never logged)

--user must be a role with privileges to create the objects the archive
contains (the schema-owning role used by db_backup.sh, not the restricted
application role).
EOF
}

host="" port="" db="" user="" input=""
while [ $# -gt 0 ]; do
    case "$1" in
        --host) host="$2"; shift 2 ;;
        --port) port="$2"; shift 2 ;;
        --db) db="$2"; shift 2 ;;
        --user) user="$2"; shift 2 ;;
        --input) input="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

for required in host port db user input; do
    if [ -z "${!required}" ]; then
        echo "ERROR: --${required//_/-} is required" >&2
        exit 2
    fi
done

if [ -z "${PGPASSWORD:-}" ]; then
    echo "ERROR: PGPASSWORD must be set in the environment" >&2
    exit 2
fi

if [ ! -f "$input" ]; then
    echo "ERROR: backup artifact not found: $input" >&2
    exit 1
fi
size="$(stat -c%s "$input" 2>/dev/null || stat -f%z "$input")"
if [ "$size" -le 0 ]; then
    echo "ERROR: backup artifact is empty: $input" >&2
    exit 1
fi
if ! pg_restore --list "$input" >/dev/null 2>&1; then
    echo "ERROR: backup artifact failed structural integrity check (pg_restore --list) -- refusing to restore a corrupt archive: $input" >&2
    exit 1
fi

echo "[restore] pg_restore ${input} -> ${user}@${host}:${port}/${db}"
pg_restore --host "$host" --port "$port" --username "$user" --dbname "$db" \
    --exit-on-error --verbose "$input"

echo "[restore] OK: ${input} -> ${db}"
