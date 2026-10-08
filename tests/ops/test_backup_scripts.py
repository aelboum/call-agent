"""Phase 2.37: hermetic sanity checks for scripts/db_backup.sh and
scripts/db_restore.sh. No PostgreSQL required -- these only check the
scripts' own static shape (fail-closed flags, no hardcoded secrets, no
secret echoed to a filename). The real end-to-end proof (backup real data,
restore into a fresh database, verify schema/RLS/data) is a manual
validation run against disposable Docker infrastructure, documented in
docs/PHASE-2.37-BACKUP-RECOVERY-FOUNDATION.md -- not repeatable in CI
without a live PostgreSQL, same reasoning as tests/integration/README.md.
"""

from __future__ import annotations

from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _read(name: str) -> str:
    # Not asserting the POSIX executable bit here: Windows' own filesystem
    # has no such bit, so `os.stat` on this platform cannot observe what
    # `chmod +x`/git's own tracked file mode set on POSIX. CI/deployment
    # platforms are POSIX; this hermetic test stays platform-agnostic.
    path = SCRIPTS_DIR / name
    assert path.exists(), f"{name} must exist under scripts/"
    return path.read_text()


def test_backup_script_fails_closed_on_strict_mode() -> None:
    text = _read("db_backup.sh")
    assert "set -euo pipefail" in text


def test_backup_script_checks_artifact_integrity() -> None:
    text = _read("db_backup.sh")
    assert "pg_restore --list" in text, (
        "must verify archive structure, not just pg_dump's exit code"
    )
    assert '[ "$size" -le 0 ]' in text, "must reject an empty artifact"


def test_backup_script_requires_password_via_env_not_argument() -> None:
    text = _read("db_backup.sh")
    assert "PGPASSWORD" in text
    assert "--password" not in text, (
        "a password must never be a CLI argument (visible in process listings)"
    )


def test_backup_filename_has_no_secret_or_credential() -> None:
    text = _read("db_backup.sh")
    assert 'outfile="${output_dir}/${db}-${timestamp}.dump"' in text


def test_restore_script_fails_closed_on_strict_mode() -> None:
    text = _read("db_restore.sh")
    assert "set -euo pipefail" in text


def test_restore_script_verifies_before_restoring() -> None:
    text = _read("db_restore.sh")
    assert "pg_restore --list" in text, (
        "must verify archive integrity before attempting a destructive restore"
    )
    assert "--exit-on-error" in text, "a partial/erroring restore must not be reported as success"


def test_restore_script_rejects_missing_or_empty_input() -> None:
    text = _read("db_restore.sh")
    assert '[ ! -f "$input" ]' in text
    assert '[ "$size" -le 0 ]' in text
