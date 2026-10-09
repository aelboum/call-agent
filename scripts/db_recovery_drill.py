#!/usr/bin/env python
"""Phase 2.43: automated disaster-recovery drill.

Wraps `scripts/db_restore.sh` (never reimplements it, same discipline
`scripts/offhost_backup.py` already follows toward `db_backup.sh`) to prove,
on a schedule a human doesn't have to remember, that a verified backup
artifact actually restores into a working database: migrations at head,
foreign keys validated, Row-Level Security enabled and forced, and the real
`tests/integration/test_domain_rls_integration.py` suite passing against the
result -- exactly the proof Phase 2.37 (`docs/PHASE-2.37-BACKUP-RECOVERY-
FOUNDATION.md` §8/§9) performed once by hand, and Phase 2.42's own
Limitations section (`docs/PHASE-2.42-OFFHOST-BACKUP-RETENTION.md` §13)
named as the gap retention does not close: "backup retention is not a
substitute for restore drills."

**The target safety boundary this module cannot itself prove** -- and does
not pretend to: hostname, database name, and connection-string shape are not
proof that a PostgreSQL instance is disposable (an operator's own laptop can
be named "localhost" too). The actual guarantee is architectural, not
something this script can observe from a connection string: the CI job that
runs this drill (`.github/workflows/ci.yml`'s `recovery-drill` job) stands up
its *own*, job-scoped, ephemeral `postgres:16-alpine` service container --
created fresh for that job, reachable by nothing else, destroyed with the
runner -- the same pattern `migrations-integration` already uses. This
module never accepts an arbitrary externally-supplied database URL and
infers safety from it; it only ever connects using `DRILL_TARGET_*`
configuration, which in practice always points at that job-scoped container.

Two checks this module *does* perform before touching anything destructive:
requiring `DRILL_MODE=true` as an explicit, unambiguous opt-in, and refusing
to restore into a target whose `app` schema is not already empty -- with no
override. Neither is the proof of disposability; both are fail-closed gates
in front of whatever target the operator/CI pointed `DRILL_TARGET_*` at.

Never reads `DATABASE_URL`/`MIGRATIONS_DATABASE_URL`/`PGPASSWORD` -- the
production-oriented names every other script in this repository uses -- for
its own restore target. `DRILL_TARGET_*` is a wholly separate configuration
namespace, structurally incapable of being the production connection by
accident of naming. Credentials never appear in the structured report this
module writes, in a raised exception's message, or in a subprocess argument
list (always environment, never `--password`).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import offhost_backup
import sqlalchemy as sa
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

_STEP_ORDER = (
    "artifact_verification",
    "target_safety_precondition",
    "restore",
    "migration_check",
    "schema_integrity_check",
    "tenant_isolation_check",
)

# Phase 2.22 (tests/integration/test_domain_rls_integration.py's own
# docstring): the one table deliberately left without RLS, so inbound-call
# tenant resolution can run before any TenantContext exists.
_RLS_EXEMPT_TABLE = "inbound_call_routes"

_PRODUCTION_CREDENTIAL_ENV_VARS = ("DATABASE_URL", "MIGRATIONS_DATABASE_URL", "PGPASSWORD")


class ConfigError(Exception):
    """Fail-closed configuration problem -- raised before any connection,
    restore, or validation is attempted."""


class DrillError(Exception):
    """One drill step failed. Carries the step name so the caller can
    record exactly where the drill stopped and mark every later step
    'skipped' rather than silently omitted from the report."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.message = message


# --------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Config:
    target_host: str
    target_port: str
    target_db: str
    target_owner_user: str
    target_owner_password: str
    target_app_user: str
    target_app_password: str
    restore_timeout_seconds: float
    validation_timeout_seconds: float


def _no_control_chars(name: str, value: str) -> str:
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ConfigError(f"{name} contains a control character or newline; rejected")
    return value


def _parse_positive_float(name: str, raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number: {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be > 0")
    return value


def _assert_no_production_credential_reuse(env: dict[str, str], values: dict[str, str]) -> None:
    """Defense in depth, not the primary safety mechanism (see module
    docstring) -- the CI job's own ephemeral, job-scoped target container is
    what actually makes the target disposable. This only catches an
    operator who copy-pasted a production value into a `DRILL_TARGET_*`
    variable by mistake."""
    target_passwords = {values["DRILL_TARGET_OWNER_PASSWORD"], values["DRILL_TARGET_APP_PASSWORD"]}
    production_password = env.get("PGPASSWORD", "")
    if production_password and production_password in target_passwords:
        raise ConfigError(
            "DRILL_TARGET_OWNER_PASSWORD/DRILL_TARGET_APP_PASSWORD must not equal the "
            "production PGPASSWORD"
        )
    target_db_path = f"/{values['DRILL_TARGET_DB']}"
    for var_name in ("DATABASE_URL", "MIGRATIONS_DATABASE_URL"):
        raw = env.get(var_name, "")
        if not raw:
            continue
        parsed = urlsplit(raw)
        same_host = parsed.hostname is not None and parsed.hostname == values["DRILL_TARGET_HOST"]
        same_db = parsed.path == target_db_path
        if same_host and same_db:
            raise ConfigError(
                f"DRILL_TARGET_HOST/DRILL_TARGET_DB must not match {var_name}'s host/database "
                "-- a drill target must never be the same database a production credential "
                "points at"
            )


def load_config(env: dict[str, str]) -> Config:
    """Fails closed: refuses to run without an explicit, unambiguous
    drill-mode opt-in and a complete target configuration. Never reads
    `DATABASE_URL`/`MIGRATIONS_DATABASE_URL`/`PGPASSWORD` -- those names are
    reserved for production and this function never looks at them for the
    restore target."""
    if env.get("DRILL_MODE", "").strip().lower() != "true":
        raise ConfigError(
            "DRILL_MODE must be explicitly set to 'true' -- this command refuses to run "
            "without an unambiguous drill-mode opt-in"
        )

    required_keys = (
        "DRILL_TARGET_HOST",
        "DRILL_TARGET_PORT",
        "DRILL_TARGET_DB",
        "DRILL_TARGET_OWNER_USER",
        "DRILL_TARGET_OWNER_PASSWORD",
        "DRILL_TARGET_APP_USER",
        "DRILL_TARGET_APP_PASSWORD",
    )
    missing = [key for key in required_keys if not env.get(key)]
    if missing:
        raise ConfigError(f"missing required drill target configuration: {missing}")
    values = {key: _no_control_chars(key, env[key]) for key in required_keys}

    _assert_no_production_credential_reuse(env, values)

    restore_timeout = _parse_positive_float(
        "DRILL_RESTORE_TIMEOUT_SECONDS", env.get("DRILL_RESTORE_TIMEOUT_SECONDS", "120")
    )
    validation_timeout = _parse_positive_float(
        "DRILL_VALIDATION_TIMEOUT_SECONDS", env.get("DRILL_VALIDATION_TIMEOUT_SECONDS", "300")
    )

    return Config(
        target_host=values["DRILL_TARGET_HOST"],
        target_port=values["DRILL_TARGET_PORT"],
        target_db=values["DRILL_TARGET_DB"],
        target_owner_user=values["DRILL_TARGET_OWNER_USER"],
        target_owner_password=values["DRILL_TARGET_OWNER_PASSWORD"],
        target_app_user=values["DRILL_TARGET_APP_USER"],
        target_app_password=values["DRILL_TARGET_APP_PASSWORD"],
        restore_timeout_seconds=restore_timeout,
        validation_timeout_seconds=validation_timeout,
    )


def _scrub_secrets(text: str, config: Config) -> str:
    scrubbed = text
    for secret in (config.target_owner_password, config.target_app_password):
        if secret:
            scrubbed = scrubbed.replace(secret, "***")
    return scrubbed


# --------------------------------------------------------------------- #
# Artifact verification (Phase 2.37/2.42 formats, reused -- never
# reimplemented)
# --------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class ArtifactInfo:
    dump_path: Path
    dump_filename: str
    backup_id: str
    sha256: str
    size_bytes: int


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(
    dump_path: Path, meta_path: Path, manifest_path: Path, manifest_checksum_path: Path
) -> ArtifactInfo:
    """Verifies the artifact byte-for-byte against its own `db_backup.sh`
    `.meta.json` sidecar AND its Phase 2.42 manifest/detached-checksum,
    before any restore is attempted. Never reads a destination, host, or
    connection field from the manifest -- there is none in the Phase 2.42
    format (`offhost_backup.build_manifest()`), and this function has no
    parameter through which one could reach the restore target even if a
    future manifest field added one."""
    if not dump_path.is_file():
        raise DrillError("artifact_verification", f"backup artifact not found: {dump_path}")
    actual_size = dump_path.stat().st_size
    if actual_size <= 0:
        raise DrillError("artifact_verification", f"backup artifact is empty: {dump_path}")
    actual_sha256 = _sha256_of(dump_path)

    if not meta_path.is_file():
        raise DrillError("artifact_verification", f"backup metadata not found: {meta_path}")
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DrillError(
            "artifact_verification", f"backup metadata is unreadable or not valid JSON: {meta_path}"
        ) from exc
    for key in ("sha256", "size_bytes"):
        if key not in meta:
            raise DrillError(
                "artifact_verification", f"backup metadata missing required field {key!r}"
            )
    if meta["sha256"] != actual_sha256 or int(meta["size_bytes"]) != actual_size:
        raise DrillError(
            "artifact_verification",
            "backup artifact does not match its own recorded checksum/size -- refusing to "
            "restore a truncated or corrupted artifact",
        )

    if not manifest_path.is_file():
        raise DrillError("artifact_verification", f"manifest not found: {manifest_path}")
    if not manifest_checksum_path.is_file():
        raise DrillError(
            "artifact_verification",
            f"manifest checksum sidecar not found: {manifest_checksum_path}",
        )
    manifest_bytes = manifest_path.read_bytes()
    checksum_line = manifest_checksum_path.read_text().split()
    if not checksum_line:
        raise DrillError("artifact_verification", "manifest checksum sidecar is empty or malformed")
    expected_manifest_digest = checksum_line[0].lower()
    actual_manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if expected_manifest_digest != actual_manifest_digest:
        raise DrillError(
            "artifact_verification",
            "manifest checksum sidecar does not match the manifest file -- refusing an "
            "inconsistent or tampered manifest",
        )
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise DrillError("artifact_verification", "manifest is not valid JSON") from exc

    required_manifest_fields = (
        "backup_format_version",
        "backup_id",
        "sha256",
        "size_bytes",
        "dump_filename",
        "database",
    )
    missing_fields = [field for field in required_manifest_fields if field not in manifest]
    if missing_fields:
        raise DrillError(
            "artifact_verification", f"manifest missing required field(s): {missing_fields}"
        )
    if manifest["backup_format_version"] != offhost_backup.MANIFEST_VERSION:
        raise DrillError(
            "artifact_verification",
            f"unsupported manifest backup_format_version: {manifest['backup_format_version']!r}",
        )
    if manifest["dump_filename"] != dump_path.name:
        raise DrillError(
            "artifact_verification",
            "manifest dump_filename does not match the supplied artifact -- refusing a "
            "mismatched manifest",
        )
    if manifest["sha256"] != actual_sha256 or int(manifest["size_bytes"]) != actual_size:
        raise DrillError(
            "artifact_verification",
            "manifest sha256/size_bytes does not match the actual artifact -- refusing an "
            "inconsistent manifest",
        )

    return ArtifactInfo(
        dump_path=dump_path,
        dump_filename=dump_path.name,
        backup_id=str(manifest["backup_id"]),
        sha256=actual_sha256,
        size_bytes=actual_size,
    )


# --------------------------------------------------------------------- #
# Target connection and safety precondition
# --------------------------------------------------------------------- #


def _build_engine(config: Config) -> sa.Engine:
    """The schema-owning role's connection to the drill target only --
    built entirely from `DRILL_TARGET_*`, never from
    `infra.db.config.get_migrations_database_config()` (which reads
    `MIGRATIONS_DATABASE_URL`) and never pooled (one-shot, same reasoning
    `migrations/env.py` gives for its own `NullPool`)."""
    url = sa.engine.URL.create(
        "postgresql+psycopg",
        username=config.target_owner_user,
        password=config.target_owner_password,
        host=config.target_host,
        port=int(config.target_port),
        database=config.target_db,
    )
    return sa.create_engine(url, poolclass=sa.pool.NullPool)


def _fetch_app_schema_object_count(conn: sa.Connection) -> int:
    result = conn.execute(
        sa.text(
            "SELECT count(*) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'app'"
        )
    ).scalar()
    return int(result or 0)


def _assert_schema_empty(object_count: int) -> None:
    """No override exists for this check, by design: a non-empty `app`
    schema means this is not a fresh target, and restoring on top of it is
    exactly the destructive-overwrite scenario Phase 2.37's own restore
    script deliberately refuses to protect against (its docstring says so
    outright) -- this drill must protect against it instead."""
    if object_count > 0:
        raise DrillError(
            "target_safety_precondition",
            f"target database's 'app' schema already contains {object_count} object(s) -- "
            "refusing to restore into a non-empty target",
        )


def _stamp_ownership_marker(conn: sa.Connection, marker_id: str) -> None:
    """Supplementary evidence only (see module docstring): proves this
    drill run connected to the target and found it empty immediately
    beforehand. It is not, by itself, proof the target is disposable
    infrastructure -- that guarantee is the CI job's ephemeral container
    lifecycle. Lives in its own `_drill` schema, never `app`, so it can
    never be mistaken for product data and never collides with what
    `pg_restore` is about to create."""
    conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS _drill"))
    conn.execute(
        sa.text(
            "CREATE TABLE IF NOT EXISTS _drill.ownership ("
            "drill_marker_id text PRIMARY KEY, created_at_utc timestamptz NOT NULL DEFAULT now())"
        )
    )
    conn.execute(
        sa.text("INSERT INTO _drill.ownership (drill_marker_id) VALUES (:marker_id)"),
        {"marker_id": marker_id},
    )
    conn.commit()


# --------------------------------------------------------------------- #
# Restore (invokes the unmodified Phase 2.37 db_restore.sh)
# --------------------------------------------------------------------- #


def _run_subprocess(
    cmd: list[str], *, env: dict[str, str], timeout: float, step: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 -- fixed argument array, no shell, caller-built env
            cmd,
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DrillError(step, f"command not found: {cmd[0]!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DrillError(step, f"timed out after {timeout}s") from exc


def _restore_env(config: Config) -> dict[str, str]:
    """A fresh, minimal environment -- never `os.environ.copy()` -- so a
    production `PGPASSWORD`/`DATABASE_URL`/`MIGRATIONS_DATABASE_URL`
    sitting in the parent process's environment cannot reach this one
    subprocess. `db_restore.sh` requires `PGPASSWORD`; it gets only the
    drill target's own."""
    env: dict[str, str] = {"PGPASSWORD": config.target_owner_password}
    for key in ("PATH", "HOME", "SystemRoot", "SystemDrive", "TEMP", "TMP", "USERPROFILE"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _run_restore(config: Config, dump_path: Path, scripts_dir: Path) -> None:
    script = scripts_dir / "db_restore.sh"
    cmd = [
        "bash",  # noqa: S607 -- resolved via PATH, same fixed invocation offhost_backup.py uses
        str(script),
        "--host",
        config.target_host,
        "--port",
        config.target_port,
        "--db",
        config.target_db,
        "--user",
        config.target_owner_user,
        "--input",
        str(dump_path),
    ]
    result = _run_subprocess(
        cmd, env=_restore_env(config), timeout=config.restore_timeout_seconds, step="restore"
    )
    if result.returncode != 0:
        raise DrillError(
            "restore",
            f"db_restore.sh exited {result.returncode}: "
            f"{_scrub_secrets(result.stderr.strip(), config)[:1000]}",
        )


# --------------------------------------------------------------------- #
# Post-restore validation: migrations, schema/relational integrity
# --------------------------------------------------------------------- #


def _expected_alembic_heads(repo_root: Path) -> list[str]:
    cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    return sorted(script.get_heads())


def _fetch_db_revision(conn: sa.Connection) -> str | None:
    has_table = conn.execute(
        sa.text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
    ).scalar()
    if has_table is not True:
        return None
    row = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchone()
    return row[0] if row else None


def _assert_migration_matches(db_revision: str | None, expected_heads: list[str]) -> None:
    if db_revision is None:
        raise DrillError(
            "migration_check",
            "restored database has no alembic_version row -- migrations did not apply",
        )
    if [db_revision] != expected_heads:
        raise DrillError(
            "migration_check",
            f"restored database is at revision {db_revision!r}, expected head {expected_heads!r}",
        )


def _fetch_invalid_constraints(conn: sa.Connection) -> list[str]:
    rows = (
        conn.execute(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE connamespace = 'app'::regnamespace AND NOT convalidated"
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _assert_no_invalid_constraints(invalid_constraint_names: list[str]) -> None:
    if invalid_constraint_names:
        raise DrillError(
            "schema_integrity_check",
            f"restored database has NOT VALID constraint(s): {invalid_constraint_names}",
        )


def _fetch_rls_table_inventory(conn: sa.Connection) -> list[tuple[str, bool, bool]]:
    rows = conn.execute(
        sa.text(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relnamespace = 'app'::regnamespace AND relkind = 'r'"
        )
    ).all()
    return [(row[0], bool(row[1]), bool(row[2])) for row in rows]


def _assert_rls_inventory_consistent(inventory: list[tuple[str, bool, bool]]) -> None:
    if not inventory:
        raise DrillError(
            "schema_integrity_check", "restored database's 'app' schema has no tables at all"
        )
    inconsistent = [
        name
        for name, rls_enabled, rls_forced in inventory
        if (rls_enabled, rls_forced)
        != ((False, False) if name == _RLS_EXEMPT_TABLE else (True, True))
    ]
    if inconsistent:
        raise DrillError(
            "schema_integrity_check",
            f"RLS enable/force flags are inconsistent on restored table(s): {inconsistent}",
        )


# --------------------------------------------------------------------- #
# Tenant isolation / application-boundary validation (real suite reused)
# --------------------------------------------------------------------- #


def _rls_subprocess_env(config: Config) -> dict[str, str]:
    """A fresh, minimal environment built entirely from `DRILL_TARGET_*` --
    never from the parent process's own `DATABASE_URL`/
    `MIGRATIONS_DATABASE_URL`, which are never read by this function or
    anything it calls. `REDIS_URL` is the same inert placeholder
    `tests/integration/README.md` documents; this suite never connects to
    it."""
    owner_url = sa.engine.URL.create(
        "postgresql+psycopg",
        username=config.target_owner_user,
        password=config.target_owner_password,
        host=config.target_host,
        port=int(config.target_port),
        database=config.target_db,
    ).render_as_string(hide_password=False)
    app_url = sa.engine.URL.create(
        "postgresql+psycopg",
        username=config.target_app_user,
        password=config.target_app_password,
        host=config.target_host,
        port=int(config.target_port),
        database=config.target_db,
    ).render_as_string(hide_password=False)

    env: dict[str, str] = {
        "DATABASE_URL": app_url,
        "MIGRATIONS_DATABASE_URL": owner_url,
        "APP_DB_USER": config.target_app_user,
        "ENVIRONMENT": "test",
        "REDIS_URL": "redis://127.0.0.1:1/0",
    }
    for key in ("PATH", "HOME", "SystemRoot", "SystemDrive", "TEMP", "TMP", "USERPROFILE", "LANG"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _run_tenant_isolation_check(config: Config, repo_root: Path) -> None:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-m",
        "integration",
        "-q",
        "tests/integration/test_domain_rls_integration.py",
    ]
    result = _run_subprocess(
        cmd,
        env=_rls_subprocess_env(config),
        timeout=config.validation_timeout_seconds,
        step="tenant_isolation_check",
        cwd=repo_root,
    )
    if result.returncode != 0:
        raise DrillError(
            "tenant_isolation_check",
            "tests/integration/test_domain_rls_integration.py failed against the restored "
            f"target (exit {result.returncode}): "
            f"{_scrub_secrets(result.stdout.strip(), config)[-2000:]}",
        )


# --------------------------------------------------------------------- #
# Orchestration and report
# --------------------------------------------------------------------- #


def _mark_remaining_skipped(results: dict[str, dict], failed_step: str) -> None:
    idx = _STEP_ORDER.index(failed_step)
    for step in _STEP_ORDER[idx + 1 :]:
        results.setdefault(
            step, {"status": "skipped", "detail": "not attempted: a prior step failed"}
        )


def run_drill(
    config: Config,
    *,
    dump_path: Path,
    meta_path: Path,
    manifest_path: Path,
    manifest_checksum_path: Path,
    scripts_dir: Path,
    repo_root: Path,
) -> dict:
    drill_id = str(uuid.uuid4())
    started_at = datetime.now(UTC)
    results: dict[str, dict] = {}
    failure: dict | None = None
    artifact: ArtifactInfo | None = None
    marker_id: str | None = None
    current_step: str = _STEP_ORDER[0]

    try:
        current_step = "artifact_verification"
        artifact = verify_artifact(dump_path, meta_path, manifest_path, manifest_checksum_path)
        results["artifact_verification"] = {
            "status": "passed",
            "detail": f"sha256 {artifact.sha256} verified against meta.json and manifest",
        }

        current_step = "target_safety_precondition"
        engine = _build_engine(config)
        with engine.connect() as conn:
            object_count = _fetch_app_schema_object_count(conn)
            _assert_schema_empty(object_count)
            marker_id = str(uuid.uuid4())
            _stamp_ownership_marker(conn, marker_id)
        results["target_safety_precondition"] = {
            "status": "passed",
            "detail": "app schema confirmed empty before restore; ownership marker stamped",
        }

        current_step = "restore"
        _run_restore(config, artifact.dump_path, scripts_dir)
        results["restore"] = {
            "status": "passed",
            "detail": f"db_restore.sh restored {artifact.dump_filename}",
        }

        current_step = "migration_check"
        with engine.connect() as conn:
            db_revision = _fetch_db_revision(conn)
            expected_heads = _expected_alembic_heads(repo_root)
            _assert_migration_matches(db_revision, expected_heads)
        head_label = expected_heads[0] if expected_heads else expected_heads
        results["migration_check"] = {
            "status": "passed",
            "detail": f"restored database at {db_revision}, matches repository head {head_label}",
        }

        current_step = "schema_integrity_check"
        with engine.connect() as conn:
            invalid_constraints = _fetch_invalid_constraints(conn)
            _assert_no_invalid_constraints(invalid_constraints)
            inventory = _fetch_rls_table_inventory(conn)
            _assert_rls_inventory_consistent(inventory)
        results["schema_integrity_check"] = {
            "status": "passed",
            "detail": f"{len(inventory)} app table(s) checked; all foreign keys validated; "
            "RLS enable/force consistent",
        }

        current_step = "tenant_isolation_check"
        _run_tenant_isolation_check(config, repo_root)
        results["tenant_isolation_check"] = {
            "status": "passed",
            "detail": "tests/integration/test_domain_rls_integration.py passed in full against "
            "the restored target",
        }
    except DrillError as exc:
        redacted = _scrub_secrets(exc.message, config)
        results[exc.step] = {"status": "failed", "detail": redacted}
        _mark_remaining_skipped(results, exc.step)
        failure = {"step": exc.step, "reason": redacted}
    except Exception as exc:  # noqa: BLE001 -- last-resort fail-closed: an
        # unanticipated exception (e.g. a raw driver error) must still
        # produce a scrubbed FAIL report rather than an unredacted traceback
        # and a missing report file.
        redacted = _scrub_secrets(f"{type(exc).__name__}: {exc}", config)
        results[current_step] = {"status": "failed", "detail": redacted}
        _mark_remaining_skipped(results, current_step)
        failure = {"step": current_step, "reason": redacted}

    finished_at = datetime.now(UTC)
    return {
        "drill_id": drill_id,
        "backup_id": artifact.backup_id if artifact else None,
        "artifact": (
            {
                "dump_filename": artifact.dump_filename,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            }
            if artifact
            else None
        ),
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "target_isolation_evidence": {
            "host": config.target_host,
            "port": config.target_port,
            "database": config.target_db,
            "ownership_marker_id": marker_id,
        },
        "steps": results,
        "verdict": "PASS" if failure is None else "FAIL",
        "failure": failure,
    }


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #


def _cmd_drill(args: argparse.Namespace) -> int:
    try:
        config = load_config(os.environ.copy())
    except ConfigError as exc:
        print(f"ERROR: configuration: {exc}", file=sys.stderr)
        return 2

    scripts_dir = Path(__file__).resolve().parent
    repo_root = scripts_dir.parent
    dump_path = Path(args.dump_path)
    meta_path = (
        Path(args.meta_path)
        if args.meta_path
        else dump_path.with_name(dump_path.name + ".meta.json")
    )
    manifest_path = (
        Path(args.manifest_path)
        if args.manifest_path
        else dump_path.with_name(dump_path.name + ".manifest.json")
    )
    manifest_checksum_path = (
        Path(args.manifest_checksum_path)
        if args.manifest_checksum_path
        else manifest_path.with_name(manifest_path.name + ".sha256")
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    report = run_drill(
        config,
        dump_path=dump_path,
        meta_path=meta_path,
        manifest_path=manifest_path,
        manifest_checksum_path=manifest_checksum_path,
        scripts_dir=scripts_dir,
        repo_root=repo_root,
    )

    exit_code = 0 if report["verdict"] == "PASS" else 1
    report_path = output_dir / f"recovery-drill-{report['drill_id']}.json"
    try:
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"ERROR: could not write drill report to {report_path}: {exc}", file=sys.stderr)
        print(f"[recovery-drill] verdict: {report['verdict']} (report NOT written)")
        return exit_code

    print(f"[recovery-drill] verdict: {report['verdict']} -- report: {report_path}")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    drill_parser = sub.add_parser(
        "drill",
        help="restore a verified backup artifact into the dedicated drill target and validate it",
    )
    drill_parser.add_argument("--dump-path", required=True)
    drill_parser.add_argument("--meta-path", default=None)
    drill_parser.add_argument("--manifest-path", default=None)
    drill_parser.add_argument("--manifest-checksum-path", default=None)
    drill_parser.add_argument("--output-dir", required=True)
    drill_parser.set_defaults(func=_cmd_drill)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
