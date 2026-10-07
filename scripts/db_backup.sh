#!/usr/bin/env bash
# Phase 2.37: minimal, deterministic PostgreSQL backup for this product's own
# database (SaaS-OS's own `infra/db/backup` is platform-team/boto3 tooling an
# import-linter contract forbids product code from depending on -- see
# docs/PHASE-0-ARCHITECTURE.md G-2 -- so this is a plain pg_dump wrapper, not
# a reimplementation of that boundary).
#
# Connects as the schema-owning role (never the restricted `saas_os_app`
# role) so the archive captures full DDL, ownership, and RLS policy
# definitions. Writes the artifact OUTSIDE any path a Docker build context or
# git commit would ever pick up -- callers must point --output-dir somewhere
# off-repo (e.g. an operator-managed backup volume).
#
# Fails closed: any missing/empty/structurally-unreadable artifact is a
# non-zero exit, not a silently "successful" empty backup.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: db_backup.sh --host H --port P --db NAME --user USER --output-dir DIR

Environment:
  PGPASSWORD   required; password for --user (never logged, never put in a filename)

Produces DIR/<db>-<UTC timestamp>.dump (pg_dump custom format, -Fc) plus a
sidecar DIR/<same name>.meta.json (postgres_version, timestamp, database,
format, size_bytes, sha256, pg_dump_version -- no secret values).
EOF
}

host="" port="" db="" user="" output_dir=""
while [ $# -gt 0 ]; do
    case "$1" in
        --host) host="$2"; shift 2 ;;
        --port) port="$2"; shift 2 ;;
        --db) db="$2"; shift 2 ;;
        --user) user="$2"; shift 2 ;;
        --output-dir) output_dir="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

for required in host port db user output_dir; do
    if [ -z "${!required}" ]; then
        echo "ERROR: --${required//_/-} is required" >&2
        exit 2
    fi
done

if [ -z "${PGPASSWORD:-}" ]; then
    echo "ERROR: PGPASSWORD must be set in the environment" >&2
    exit 2
fi

mkdir -p "$output_dir"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
outfile="${output_dir}/${db}-${timestamp}.dump"
metafile="${outfile}.meta.json"

echo "[backup] pg_dump -Fc ${user}@${host}:${port}/${db} -> ${outfile}"
pg_dump --host "$host" --port "$port" --username "$user" --dbname "$db" \
    --format=custom --file="$outfile"

# Fail closed: existence, non-empty, and structural readability are all
# checked explicitly -- "pg_dump exited 0" alone is not treated as proof.
if [ ! -f "$outfile" ]; then
    echo "ERROR: backup artifact was not created: $outfile" >&2
    exit 1
fi
size="$(stat -c%s "$outfile" 2>/dev/null || stat -f%z "$outfile")"
if [ "$size" -le 0 ]; then
    echo "ERROR: backup artifact is empty: $outfile" >&2
    exit 1
fi
if ! pg_restore --list "$outfile" >/dev/null 2>&1; then
    echo "ERROR: backup artifact failed structural integrity check (pg_restore --list): $outfile" >&2
    exit 1
fi

sha256="$(sha256sum "$outfile" | awk '{print $1}')"
pg_version="$(psql --host "$host" --port "$port" --username "$user" --dbname "$db" \
    -t -A -c 'show server_version')"
pg_dump_version="$(pg_dump --version)"

cat > "$metafile" <<EOF
{
  "database": "${db}",
  "timestamp_utc": "${timestamp}",
  "format": "custom (pg_dump -Fc)",
  "size_bytes": ${size},
  "sha256": "${sha256}",
  "postgres_server_version": "${pg_version}",
  "pg_dump_version": "${pg_dump_version}"
}
EOF

echo "[backup] OK: ${outfile} (${size} bytes, sha256 ${sha256})"
echo "[backup] metadata: ${metafile}"
