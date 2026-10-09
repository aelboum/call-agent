"""Phase 2.43: hermetic and subprocess-boundary tests for
`scripts/db_recovery_drill.py`.

Config validation, artifact verification, safety-precondition decisions, and
report/credential-isolation behaviour are pure logic over local files/dicts
and mocked subprocess/connection boundaries -- no real PostgreSQL needed to
prove them correct, same reasoning as `tests/ops/test_offhost_backup.py`'s
own docstring. The real end-to-end proof (an actual backup restored into an
actual disposable PostgreSQL instance, migrations/RLS verified for real) is
`tests/integration/test_recovery_drill_integration.py`, excluded from the
default run the same way every other `-m integration` test is.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import db_recovery_drill as m  # noqa: E402
import offhost_backup  # noqa: E402

# --------------------------------------------------------------------- #
# Config: drill-mode opt-in and target configuration
# --------------------------------------------------------------------- #


def _target_env(**overrides: str) -> dict[str, str]:
    env = {
        "DRILL_MODE": "true",
        "DRILL_TARGET_HOST": "127.0.0.1",
        "DRILL_TARGET_PORT": "5433",
        "DRILL_TARGET_DB": "voiceagent",
        "DRILL_TARGET_OWNER_USER": "saas_os",
        "DRILL_TARGET_OWNER_PASSWORD": "drill-owner-pw",  # pragma: allowlist secret
        "DRILL_TARGET_APP_USER": "saas_os_app",
        "DRILL_TARGET_APP_PASSWORD": "drill-app-pw",  # pragma: allowlist secret
    }
    env.update(overrides)
    return env


def test_config_rejects_missing_drill_mode() -> None:
    with pytest.raises(m.ConfigError, match="DRILL_MODE"):
        m.load_config(_target_env(DRILL_MODE=""))


def test_config_rejects_non_true_drill_mode() -> None:
    with pytest.raises(m.ConfigError, match="DRILL_MODE"):
        m.load_config(_target_env(DRILL_MODE="yes"))


def test_config_valid_minimal_is_accepted() -> None:
    config = m.load_config(_target_env())
    assert config.target_host == "127.0.0.1"
    assert config.target_db == "voiceagent"
    assert config.restore_timeout_seconds == 120
    assert config.validation_timeout_seconds == 300


@pytest.mark.parametrize(
    "missing_key",
    [
        "DRILL_TARGET_HOST",
        "DRILL_TARGET_PORT",
        "DRILL_TARGET_DB",
        "DRILL_TARGET_OWNER_USER",
        "DRILL_TARGET_OWNER_PASSWORD",
        "DRILL_TARGET_APP_USER",
        "DRILL_TARGET_APP_PASSWORD",
    ],
)
def test_config_rejects_missing_target_field(missing_key: str) -> None:
    env = _target_env()
    env[missing_key] = ""
    with pytest.raises(m.ConfigError, match="missing required drill target configuration"):
        m.load_config(env)


def test_config_rejects_control_characters_in_target_field() -> None:
    with pytest.raises(m.ConfigError, match="control character"):
        m.load_config(_target_env(DRILL_TARGET_HOST="127.0.0.1\nX-Injected: 1"))


def test_config_rejects_timeout_that_is_not_a_number() -> None:
    with pytest.raises(m.ConfigError, match="DRILL_RESTORE_TIMEOUT_SECONDS"):
        m.load_config(_target_env(DRILL_RESTORE_TIMEOUT_SECONDS="soon"))


def test_config_rejects_non_positive_timeout() -> None:
    with pytest.raises(m.ConfigError, match="must be > 0"):
        m.load_config(_target_env(DRILL_VALIDATION_TIMEOUT_SECONDS="0"))


# --------------------------------------------------------------------- #
# Config: production credentials never reused as target credentials
# --------------------------------------------------------------------- #


def test_config_rejects_target_password_equal_to_production_pgpassword() -> None:
    env = _target_env(DRILL_TARGET_OWNER_PASSWORD="shared-secret")  # noqa: S106
    env["PGPASSWORD"] = "shared-secret"
    with pytest.raises(m.ConfigError, match="production PGPASSWORD"):
        m.load_config(env)


def test_config_rejects_target_matching_production_database_url_host_and_db() -> None:
    env = _target_env(DRILL_TARGET_HOST="prod-host", DRILL_TARGET_DB="voiceagent")
    env["DATABASE_URL"] = "postgresql+psycopg://u:p@prod-host:5432/voiceagent"
    with pytest.raises(m.ConfigError, match="must not match DATABASE_URL"):
        m.load_config(env)


def test_config_allows_target_sharing_only_host_not_database() -> None:
    env = _target_env(DRILL_TARGET_HOST="prod-host", DRILL_TARGET_DB="drill-only-db")
    env["DATABASE_URL"] = "postgresql+psycopg://u:p@prod-host:5432/voiceagent"
    m.load_config(env)  # must not raise: different database on the same host is not a collision


def test_config_ignores_absent_production_vars() -> None:
    m.load_config(_target_env())  # no DATABASE_URL/MIGRATIONS_DATABASE_URL/PGPASSWORD set at all


# --------------------------------------------------------------------- #
# Credential isolation: subprocess environments never leak production vars
# --------------------------------------------------------------------- #

_PRODUCTION_DATABASE_URL = "postgresql+psycopg://prod:x@prod-host/prod"  # pragma: allowlist secret
_PRODUCTION_MIGRATIONS_URL = (
    "postgresql+psycopg://owner:x@prod-host/prod"  # pragma: allowlist secret
)
_PRODUCTION_PGPASSWORD = "real-production-password"  # pragma: allowlist secret


def _poison_production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", _PRODUCTION_DATABASE_URL)
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", _PRODUCTION_MIGRATIONS_URL)
    monkeypatch.setenv("PGPASSWORD", _PRODUCTION_PGPASSWORD)


def test_restore_env_never_contains_production_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    _poison_production_env(monkeypatch)
    config = m.load_config(_target_env())
    env = m._restore_env(config)
    assert env["PGPASSWORD"] == config.target_owner_password
    assert "DATABASE_URL" not in env
    assert "MIGRATIONS_DATABASE_URL" not in env
    assert _PRODUCTION_PGPASSWORD not in env.values()


def test_rls_subprocess_env_never_contains_production_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _poison_production_env(monkeypatch)
    config = m.load_config(_target_env())
    env = m._rls_subprocess_env(config)
    assert "prod-host" not in env["DATABASE_URL"]
    assert "prod-host" not in env["MIGRATIONS_DATABASE_URL"]
    assert config.target_host in env["DATABASE_URL"]
    assert config.target_app_user in env["DATABASE_URL"]
    assert config.target_owner_user in env["MIGRATIONS_DATABASE_URL"]
    assert "PGPASSWORD" not in env
    assert _PRODUCTION_PGPASSWORD not in " ".join(env.values())


# --------------------------------------------------------------------- #
# Artifact verification
# --------------------------------------------------------------------- #


def _write_valid_artifact(tmp_path: Path, *, name: str = "voiceagent-20261009T000000Z") -> Path:
    dump_path = tmp_path / f"{name}.dump"
    dump_path.write_bytes(b"not a real pg_dump archive, just deterministic bytes" * 10)
    sha256 = hashlib.sha256(dump_path.read_bytes()).hexdigest()
    size_bytes = dump_path.stat().st_size

    meta_path = dump_path.with_name(dump_path.name + ".meta.json")
    meta_path.write_text(json.dumps({"sha256": sha256, "size_bytes": size_bytes}))

    manifest_path = dump_path.with_name(dump_path.name + ".manifest.json")
    manifest = {
        "backup_format_version": offhost_backup.MANIFEST_VERSION,
        "backup_id": name,
        "created_at_utc": "20261009T000000Z",
        "database": "voiceagent",
        "dump_filename": dump_path.name,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "format": "custom (pg_dump -Fc)",
        "migration_revision": "0012_inbound_routing",
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
    manifest_path.write_bytes(manifest_bytes)

    checksum_path = manifest_path.with_name(manifest_path.name + ".sha256")
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    checksum_path.write_text(f"{digest}  {manifest_path.name}\n")

    return dump_path


def _artifact_paths(dump_path: Path) -> tuple[Path, Path, Path]:
    meta_path = dump_path.with_name(dump_path.name + ".meta.json")
    manifest_path = dump_path.with_name(dump_path.name + ".manifest.json")
    checksum_path = manifest_path.with_name(manifest_path.name + ".sha256")
    return meta_path, manifest_path, checksum_path


def test_verify_artifact_accepts_a_consistent_artifact(tmp_path: Path) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    meta_path, manifest_path, checksum_path = _artifact_paths(dump_path)

    artifact = m.verify_artifact(dump_path, meta_path, manifest_path, checksum_path)

    assert artifact.dump_filename == dump_path.name
    assert artifact.backup_id == "voiceagent-20261009T000000Z"
    assert artifact.size_bytes == dump_path.stat().st_size


def test_verify_artifact_rejects_missing_dump(tmp_path: Path) -> None:
    dump_path = tmp_path / "missing.dump"
    meta_path, manifest_path, checksum_path = _artifact_paths(dump_path)
    with pytest.raises(m.DrillError, match="not found"):
        m.verify_artifact(dump_path, meta_path, manifest_path, checksum_path)


def test_verify_artifact_rejects_truncated_dump(tmp_path: Path) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    meta_path, manifest_path, checksum_path = _artifact_paths(dump_path)
    dump_path.write_bytes(dump_path.read_bytes()[:5])  # truncate after meta/manifest were computed

    with pytest.raises(m.DrillError, match="checksum/size"):
        m.verify_artifact(dump_path, meta_path, manifest_path, checksum_path)


def test_verify_artifact_rejects_checksum_mismatch_in_meta(tmp_path: Path) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    meta_path, manifest_path, checksum_path = _artifact_paths(dump_path)
    meta = json.loads(meta_path.read_text())
    meta["sha256"] = "0" * 64
    meta_path.write_text(json.dumps(meta))

    with pytest.raises(m.DrillError, match="checksum/size"):
        m.verify_artifact(dump_path, meta_path, manifest_path, checksum_path)


def test_verify_artifact_rejects_missing_manifest(tmp_path: Path) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    meta_path, manifest_path, checksum_path = _artifact_paths(dump_path)
    manifest_path.unlink()

    with pytest.raises(m.DrillError, match="manifest not found"):
        m.verify_artifact(dump_path, meta_path, manifest_path, checksum_path)


def test_verify_artifact_rejects_tampered_manifest_checksum(tmp_path: Path) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    meta_path, manifest_path, checksum_path = _artifact_paths(dump_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["migration_revision"] = "tampered"
    manifest_path.write_text(json.dumps(manifest))  # checksum sidecar now stale

    with pytest.raises(m.DrillError, match="does not match the manifest file"):
        m.verify_artifact(dump_path, meta_path, manifest_path, checksum_path)


def test_verify_artifact_rejects_invalid_manifest_json(tmp_path: Path) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    meta_path, manifest_path, checksum_path = _artifact_paths(dump_path)
    manifest_path.write_bytes(b"{not json")
    checksum_path.write_text(f"{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}  x\n")

    with pytest.raises(m.DrillError, match="not valid JSON"):
        m.verify_artifact(dump_path, meta_path, manifest_path, checksum_path)


def test_verify_artifact_rejects_missing_manifest_field(tmp_path: Path) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    meta_path, manifest_path, checksum_path = _artifact_paths(dump_path)
    manifest = json.loads(manifest_path.read_text())
    del manifest["backup_id"]
    manifest_bytes = json.dumps(manifest).encode()
    manifest_path.write_bytes(manifest_bytes)
    checksum_path.write_text(f"{hashlib.sha256(manifest_bytes).hexdigest()}  x\n")

    with pytest.raises(m.DrillError, match="missing required field"):
        m.verify_artifact(dump_path, meta_path, manifest_path, checksum_path)


def test_verify_artifact_rejects_unsupported_manifest_version(tmp_path: Path) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    meta_path, manifest_path, checksum_path = _artifact_paths(dump_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["backup_format_version"] = "some-other-format/9"
    manifest_bytes = json.dumps(manifest).encode()
    manifest_path.write_bytes(manifest_bytes)
    checksum_path.write_text(f"{hashlib.sha256(manifest_bytes).hexdigest()}  x\n")

    with pytest.raises(m.DrillError, match="unsupported manifest"):
        m.verify_artifact(dump_path, meta_path, manifest_path, checksum_path)


def test_verify_artifact_rejects_manifest_pointing_at_a_different_filename(tmp_path: Path) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    meta_path, manifest_path, checksum_path = _artifact_paths(dump_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["dump_filename"] = "some-other-backup.dump"
    manifest_bytes = json.dumps(manifest).encode()
    manifest_path.write_bytes(manifest_bytes)
    checksum_path.write_text(f"{hashlib.sha256(manifest_bytes).hexdigest()}  x\n")

    with pytest.raises(m.DrillError, match="does not match the supplied artifact"):
        m.verify_artifact(dump_path, meta_path, manifest_path, checksum_path)


def test_verify_artifact_manifest_has_no_connection_or_destination_field() -> None:
    """The Phase 2.42 manifest format carries no host/db/destination field
    at all (`offhost_backup.build_manifest()`); `verify_artifact()` has no
    parameter through which one could reach the restore target even if a
    future manifest accidentally grew one -- it returns only an
    `ArtifactInfo`, never a connection target."""
    import inspect

    fields = set(inspect.signature(m.ArtifactInfo).parameters)
    assert fields == {"dump_path", "dump_filename", "backup_id", "sha256", "size_bytes"}


# --------------------------------------------------------------------- #
# Target safety precondition: non-empty schema rejected, no override
# --------------------------------------------------------------------- #


def test_assert_schema_empty_accepts_zero_objects() -> None:
    m._assert_schema_empty(0)  # must not raise


def test_assert_schema_empty_rejects_any_existing_object() -> None:
    with pytest.raises(m.DrillError, match="already contains"):
        m._assert_schema_empty(1)


def test_assert_schema_empty_has_no_override_parameter() -> None:
    import inspect

    assert list(inspect.signature(m._assert_schema_empty).parameters) == ["object_count"]


# --------------------------------------------------------------------- #
# Migration-revision classification
# --------------------------------------------------------------------- #


def test_assert_migration_matches_accepts_matching_single_head() -> None:
    m._assert_migration_matches("0012_inbound_routing", ["0012_inbound_routing"])


def test_assert_migration_matches_rejects_no_revision_at_all() -> None:
    with pytest.raises(m.DrillError, match="no alembic_version row"):
        m._assert_migration_matches(None, ["0012_inbound_routing"])


def test_assert_migration_matches_rejects_stale_revision() -> None:
    with pytest.raises(m.DrillError, match="expected head"):
        m._assert_migration_matches("0007_follow_up_execution", ["0012_inbound_routing"])


# --------------------------------------------------------------------- #
# Schema/relational integrity classification
# --------------------------------------------------------------------- #


def test_assert_no_invalid_constraints_accepts_empty_list() -> None:
    m._assert_no_invalid_constraints([])


def test_assert_no_invalid_constraints_rejects_any_not_valid_constraint() -> None:
    with pytest.raises(m.DrillError, match="NOT VALID"):
        m._assert_no_invalid_constraints(["agents_tenant_id_fkey"])


def test_assert_rls_inventory_rejects_empty_schema() -> None:
    with pytest.raises(m.DrillError, match="no tables at all"):
        m._assert_rls_inventory_consistent([])


def test_assert_rls_inventory_accepts_consistent_tables_with_known_exception() -> None:
    m._assert_rls_inventory_consistent(
        [
            ("agents", True, True),
            ("phone_numbers", True, True),
            (m._RLS_EXEMPT_TABLE, False, False),
        ]
    )


def test_assert_rls_inventory_rejects_a_table_missing_rls() -> None:
    with pytest.raises(m.DrillError, match="inconsistent"):
        m._assert_rls_inventory_consistent([("agents", False, False)])


def test_assert_rls_inventory_rejects_exempt_table_accidentally_protected() -> None:
    """If a future migration enables RLS on the one deliberate exception,
    that is a real schema drift this check must also catch, not silently
    accept as 'better than expected'."""
    with pytest.raises(m.DrillError, match="inconsistent"):
        m._assert_rls_inventory_consistent([(m._RLS_EXEMPT_TABLE, True, True)])


# --------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------- #


def test_scrub_secrets_removes_both_target_passwords() -> None:
    config = m.load_config(_target_env())
    text = (
        f"connection refused: user={config.target_owner_user} "
        f"password={config.target_owner_password} and {config.target_app_password}"
    )
    scrubbed = m._scrub_secrets(text, config)
    assert config.target_owner_password not in scrubbed
    assert config.target_app_password not in scrubbed
    assert "***" in scrubbed


# --------------------------------------------------------------------- #
# Subprocess boundary: timeouts, failures, no fallback
# --------------------------------------------------------------------- #


def test_run_subprocess_raises_drill_error_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["x"], timeout=1)

    monkeypatch.setattr(m.subprocess, "run", _raise_timeout)
    with pytest.raises(m.DrillError, match="timed out") as exc_info:
        m._run_subprocess(["x"], env={}, timeout=1, step="restore")
    assert exc_info.value.step == "restore"


def test_run_subprocess_raises_drill_error_when_command_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_not_found(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(m.subprocess, "run", _raise_not_found)
    with pytest.raises(m.DrillError, match="command not found"):
        m._run_subprocess(["nonexistent-binary"], env={}, timeout=1, step="restore")


def test_run_restore_raises_on_nonzero_exit_and_redacts_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = m.load_config(_target_env(DRILL_TARGET_OWNER_PASSWORD="super-secret-pw"))  # noqa: S106

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout="", stderr="auth failed for super-secret-pw"
        )

    monkeypatch.setattr(m.subprocess, "run", _fake_run)
    with pytest.raises(m.DrillError) as exc_info:
        m._run_restore(config, tmp_path / "x.dump", tmp_path)
    assert "super-secret-pw" not in str(exc_info.value)
    assert exc_info.value.step == "restore"


# --------------------------------------------------------------------- #
# Full orchestration: step skipping, PASS/FAIL classification, no
# destructive call after a failed precondition, no retry/fallback
# --------------------------------------------------------------------- #


class _FakeConnCtx:
    def __enter__(self) -> str:
        return "fake-conn"

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakeEngine:
    def connect(self) -> _FakeConnCtx:
        return _FakeConnCtx()


def _drill_kwargs(tmp_path: Path, dump_path: Path) -> dict:
    meta_path, manifest_path, checksum_path = _artifact_paths(dump_path)
    return {
        "dump_path": dump_path,
        "meta_path": meta_path,
        "manifest_path": manifest_path,
        "manifest_checksum_path": checksum_path,
        "scripts_dir": tmp_path,
        "repo_root": Path(__file__).resolve().parents[2],
    }


def test_run_drill_fails_closed_before_any_destructive_subprocess_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-empty target must stop the drill before `db_restore.sh` (or
    anything else) is ever invoked as a subprocess."""
    dump_path = _write_valid_artifact(tmp_path)
    config = m.load_config(_target_env())

    subprocess_calls: list[list[str]] = []
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    monkeypatch.setattr(m, "_build_engine", lambda config: _FakeEngine())
    monkeypatch.setattr(m, "_fetch_app_schema_object_count", lambda conn: 3)
    monkeypatch.setattr(m, "_stamp_ownership_marker", lambda conn, marker_id: None)

    report = m.run_drill(config, **_drill_kwargs(tmp_path, dump_path))

    assert report["verdict"] == "FAIL"
    assert report["failure"]["step"] == "target_safety_precondition"
    assert report["steps"]["artifact_verification"]["status"] == "passed"
    assert report["steps"]["target_safety_precondition"]["status"] == "failed"
    assert report["steps"]["restore"]["status"] == "skipped"
    assert report["steps"]["migration_check"]["status"] == "skipped"
    assert report["steps"]["tenant_isolation_check"]["status"] == "skipped"
    assert subprocess_calls == []  # no destructive (or any) subprocess ever ran


def test_run_drill_stops_after_restore_failure_and_never_runs_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    config = m.load_config(_target_env())

    subprocess_calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="restore failed")

    monkeypatch.setattr(m.subprocess, "run", _fake_run)
    monkeypatch.setattr(m, "_build_engine", lambda config: _FakeEngine())
    monkeypatch.setattr(m, "_fetch_app_schema_object_count", lambda conn: 0)
    monkeypatch.setattr(m, "_stamp_ownership_marker", lambda conn, marker_id: None)

    report = m.run_drill(config, **_drill_kwargs(tmp_path, dump_path))

    assert report["verdict"] == "FAIL"
    assert report["failure"]["step"] == "restore"
    assert report["steps"]["target_safety_precondition"]["status"] == "passed"
    assert report["steps"]["migration_check"]["status"] == "skipped"
    assert report["steps"]["tenant_isolation_check"]["status"] == "skipped"
    # Exactly one subprocess call (the failed restore) -- no retry, no
    # fallback to any other target, and no later step's subprocess ran.
    assert len(subprocess_calls) == 1


def test_run_drill_fails_on_migration_mismatch_and_skips_later_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    config = m.load_config(_target_env())

    restore_calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        restore_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(m.subprocess, "run", _fake_run)
    monkeypatch.setattr(m, "_build_engine", lambda config: _FakeEngine())
    monkeypatch.setattr(m, "_fetch_app_schema_object_count", lambda conn: 0)
    monkeypatch.setattr(m, "_stamp_ownership_marker", lambda conn, marker_id: None)
    monkeypatch.setattr(m, "_fetch_db_revision", lambda conn: "0007_follow_up_execution")
    monkeypatch.setattr(m, "_expected_alembic_heads", lambda repo_root: ["0012_inbound_routing"])

    report = m.run_drill(config, **_drill_kwargs(tmp_path, dump_path))

    assert report["verdict"] == "FAIL"
    assert report["failure"]["step"] == "migration_check"
    assert report["steps"]["restore"]["status"] == "passed"
    assert report["steps"]["schema_integrity_check"]["status"] == "skipped"
    assert report["steps"]["tenant_isolation_check"]["status"] == "skipped"
    # The restore ran exactly once; the tenant-isolation pytest subprocess
    # never ran because migration_check failed first.
    assert len(restore_calls) == 1


def test_run_drill_fails_on_tenant_isolation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    config = m.load_config(_target_env())

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "pytest" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="2 failed", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(m.subprocess, "run", _fake_run)
    monkeypatch.setattr(m, "_build_engine", lambda config: _FakeEngine())
    monkeypatch.setattr(m, "_fetch_app_schema_object_count", lambda conn: 0)
    monkeypatch.setattr(m, "_stamp_ownership_marker", lambda conn, marker_id: None)
    monkeypatch.setattr(m, "_fetch_db_revision", lambda conn: "0012_inbound_routing")
    monkeypatch.setattr(m, "_expected_alembic_heads", lambda repo_root: ["0012_inbound_routing"])
    monkeypatch.setattr(m, "_fetch_invalid_constraints", lambda conn: [])
    monkeypatch.setattr(m, "_fetch_rls_table_inventory", lambda conn: [("agents", True, True)])

    report = m.run_drill(config, **_drill_kwargs(tmp_path, dump_path))

    assert report["verdict"] == "FAIL"
    assert report["failure"]["step"] == "tenant_isolation_check"
    assert report["steps"]["schema_integrity_check"]["status"] == "passed"


def test_run_drill_catches_unexpected_non_drill_error_and_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A raw, unanticipated exception (e.g. a driver error that is not a
    `DrillError`) must still produce a written, scrubbed FAIL report at the
    step it occurred in -- never propagate out of `run_drill` uncaught,
    which would both skip writing any report and risk an unredacted
    secret reaching stderr via the default traceback."""
    dump_path = _write_valid_artifact(tmp_path)
    config = m.load_config(_target_env())

    def _raise_raw_driver_error(config: m.Config) -> _FakeEngine:
        raise RuntimeError(f"connection refused, password={config.target_owner_password}")

    monkeypatch.setattr(m, "_build_engine", _raise_raw_driver_error)

    report = m.run_drill(config, **_drill_kwargs(tmp_path, dump_path))

    assert report["verdict"] == "FAIL"
    assert report["failure"]["step"] == "target_safety_precondition"
    assert report["steps"]["artifact_verification"]["status"] == "passed"
    assert report["steps"]["target_safety_precondition"]["status"] == "failed"
    assert report["steps"]["restore"]["status"] == "skipped"
    serialized = json.dumps(report)
    assert config.target_owner_password not in serialized
    assert "RuntimeError" in report["steps"]["target_safety_precondition"]["detail"]


def test_run_drill_passes_when_every_step_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dump_path = _write_valid_artifact(tmp_path)
    config = m.load_config(_target_env())

    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(m, "_build_engine", lambda config: _FakeEngine())
    monkeypatch.setattr(m, "_fetch_app_schema_object_count", lambda conn: 0)
    monkeypatch.setattr(m, "_stamp_ownership_marker", lambda conn, marker_id: None)
    monkeypatch.setattr(m, "_fetch_db_revision", lambda conn: "0012_inbound_routing")
    monkeypatch.setattr(m, "_expected_alembic_heads", lambda repo_root: ["0012_inbound_routing"])
    monkeypatch.setattr(m, "_fetch_invalid_constraints", lambda conn: [])
    monkeypatch.setattr(m, "_fetch_rls_table_inventory", lambda conn: [("agents", True, True)])

    report = m.run_drill(config, **_drill_kwargs(tmp_path, dump_path))

    assert report["verdict"] == "PASS"
    assert report["failure"] is None
    assert all(step["status"] == "passed" for step in report["steps"].values())
    assert report["backup_id"] == "voiceagent-20261009T000000Z"
    assert report["target_isolation_evidence"]["host"] == config.target_host
    assert report["target_isolation_evidence"]["ownership_marker_id"] is not None
    assert config.target_owner_password not in json.dumps(report)
    assert config.target_app_password not in json.dumps(report)


def test_run_drill_report_never_contains_either_target_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Failure path too: a raised error quoting subprocess output must
    still come out of the report with both passwords scrubbed."""
    dump_path = _write_valid_artifact(tmp_path)
    config = m.load_config(_target_env())

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            returncode=1,
            stdout="",
            stderr=f"fatal: password authentication failed for {config.target_owner_password}",
        )

    monkeypatch.setattr(m.subprocess, "run", _fake_run)
    monkeypatch.setattr(m, "_build_engine", lambda config: _FakeEngine())
    monkeypatch.setattr(m, "_fetch_app_schema_object_count", lambda conn: 0)
    monkeypatch.setattr(m, "_stamp_ownership_marker", lambda conn, marker_id: None)

    report = m.run_drill(config, **_drill_kwargs(tmp_path, dump_path))

    assert report["verdict"] == "FAIL"
    serialized = json.dumps(report)
    assert config.target_owner_password not in serialized
    assert config.target_app_password not in serialized
    assert "***" in serialized


# --------------------------------------------------------------------- #
# CLI entry point (`_cmd_drill`): argument handling, path defaulting vs.
# explicit overrides, safe failure for invalid/missing input, and the
# structured report it writes on disk -- all hermetic, no real PostgreSQL.
# --------------------------------------------------------------------- #


def _apply_full_pass_monkeypatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same combination `test_run_drill_passes_when_every_step_succeeds`
    uses: every destructive/DB boundary mocked so `run_drill()` reaches a
    real PASS verdict without any PostgreSQL instance."""
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(m, "_build_engine", lambda config: _FakeEngine())
    monkeypatch.setattr(m, "_fetch_app_schema_object_count", lambda conn: 0)
    monkeypatch.setattr(m, "_stamp_ownership_marker", lambda conn, marker_id: None)
    monkeypatch.setattr(m, "_fetch_db_revision", lambda conn: "0012_inbound_routing")
    monkeypatch.setattr(m, "_expected_alembic_heads", lambda repo_root: ["0012_inbound_routing"])
    monkeypatch.setattr(m, "_fetch_invalid_constraints", lambda conn: [])
    monkeypatch.setattr(m, "_fetch_rls_table_inventory", lambda conn: [("agents", True, True)])


def _drill_args(
    dump_path: Path,
    output_dir: Path,
    *,
    meta_path: str | None = None,
    manifest_path: str | None = None,
    manifest_checksum_path: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        dump_path=str(dump_path),
        meta_path=meta_path,
        manifest_path=manifest_path,
        manifest_checksum_path=manifest_checksum_path,
        output_dir=str(output_dir),
    )


def _read_only_report(output_dir: Path) -> dict:
    reports = list(output_dir.glob("recovery-drill-*.json"))
    assert len(reports) == 1, f"expected exactly one report file, found {reports}"
    return json.loads(reports[0].read_text())


def test_cmd_drill_defaults_meta_manifest_checksum_paths_from_dump_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no `--meta-path`/`--manifest-path`/`--manifest-checksum-path`,
    `_cmd_drill` must derive all three from `--dump-path` -- and the
    derivation must actually be correct, not just non-crashing: the whole
    drill reaching PASS (every later step mocked to succeed) proves the
    artifact was found and verified at the derived locations."""
    monkeypatch.setattr(os, "environ", _target_env())
    _apply_full_pass_monkeypatches(monkeypatch)
    dump_path = _write_valid_artifact(tmp_path)
    output_dir = tmp_path / "out"

    exit_code = m._cmd_drill(_drill_args(dump_path, output_dir))

    assert exit_code == 0
    report = _read_only_report(output_dir)
    assert report["verdict"] == "PASS"
    assert report["steps"]["artifact_verification"]["status"] == "passed"


def test_cmd_drill_honors_explicit_path_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit `--meta-path`/`--manifest-path`/`--manifest-checksum-path`
    must be used INSTEAD of the derived defaults: the three sidecar files
    are moved away from where default derivation would look, so the drill
    can only reach PASS by actually honoring the explicit overrides."""
    monkeypatch.setattr(os, "environ", _target_env())
    _apply_full_pass_monkeypatches(monkeypatch)

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    dump_path = _write_valid_artifact(artifacts_dir)
    default_meta, default_manifest, default_checksum = _artifact_paths(dump_path)

    custom_dir = tmp_path / "custom-sidecars"
    custom_dir.mkdir()
    custom_meta = custom_dir / "meta.json"
    custom_manifest = custom_dir / "manifest.json"
    custom_checksum = custom_dir / "manifest.json.sha256"
    shutil.move(str(default_meta), str(custom_meta))
    shutil.move(str(default_manifest), str(custom_manifest))
    shutil.move(str(default_checksum), str(custom_checksum))
    assert not default_meta.exists()
    assert not default_manifest.exists()
    assert not default_checksum.exists()

    output_dir = tmp_path / "out"
    exit_code = m._cmd_drill(
        _drill_args(
            dump_path,
            output_dir,
            meta_path=str(custom_meta),
            manifest_path=str(custom_manifest),
            manifest_checksum_path=str(custom_checksum),
        )
    )

    assert exit_code == 0
    report = _read_only_report(output_dir)
    assert report["verdict"] == "PASS"
    assert report["steps"]["artifact_verification"]["status"] == "passed"


def test_cmd_drill_exits_2_on_missing_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid/missing input at the configuration boundary (no
    `DRILL_MODE` opt-in) must fail closed with a distinct exit code and
    never reach the filesystem -- `output_dir` is never created, and no
    drill ever runs."""
    monkeypatch.setattr(os, "environ", {})
    dump_path = _write_valid_artifact(tmp_path)
    output_dir = tmp_path / "out"

    exit_code = m._cmd_drill(_drill_args(dump_path, output_dir))

    assert exit_code == 2
    assert "ERROR: configuration" in capsys.readouterr().err
    assert not output_dir.exists()


def test_cmd_drill_fails_closed_on_missing_dump_file_with_structured_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing/invalid `--dump-path` must still produce exit code 1 and
    a structured, written FAIL report naming the real step -- not a bare
    traceback and not a silently-skipped report."""
    monkeypatch.setattr(os, "environ", _target_env())
    dump_path = tmp_path / "does-not-exist.dump"
    output_dir = tmp_path / "out"

    exit_code = m._cmd_drill(_drill_args(dump_path, output_dir))

    assert exit_code == 1
    report = _read_only_report(output_dir)
    assert report["verdict"] == "FAIL"
    assert report["failure"]["step"] == "artifact_verification"
    assert "verdict: FAIL" in capsys.readouterr().out


def test_cmd_drill_falls_back_cleanly_when_report_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The report-write failure path (`OSError` writing the JSON report
    itself) must still return the verdict's exit code and print a clear
    fallback message -- never raise out of `_cmd_drill`."""
    fixed_id = uuid.UUID(int=0)
    monkeypatch.setattr(m.uuid, "uuid4", lambda: fixed_id)
    monkeypatch.setattr(os, "environ", _target_env())
    dump_path = tmp_path / "does-not-exist.dump"
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    # Pre-occupy the exact report path with a directory so `write_text`
    # raises `IsADirectoryError` (an `OSError` subclass).
    (output_dir / f"recovery-drill-{fixed_id}.json").mkdir()

    exit_code = m._cmd_drill(_drill_args(dump_path, output_dir))

    assert exit_code == 1  # the FAIL verdict's exit code, computed before the write attempt
    captured = capsys.readouterr()
    assert "could not write drill report" in captured.err
    assert "report NOT written" in captured.out
