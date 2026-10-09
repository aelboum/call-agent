"""Phase 2.43: real-PostgreSQL end-to-end proof of `scripts/db_recovery_drill.py`.

Produces a real backup of the SOURCE instance every other test in this
directory already requires (`DATABASE_URL`/`MIGRATIONS_DATABASE_URL`, see
`tests/integration/README.md`), then restores it into a second, completely
separate, disposable PostgreSQL instance and runs the full drill against
that target -- the one proof this repository's unit tests
(`tests/ops/test_recovery_drill.py`) cannot give: that an actual `pg_dump`
archive produced by `scripts/db_backup.sh` actually restores for real, that
`alembic_version` actually matches head afterward, and that
`tests/integration/test_domain_rls_integration.py` actually passes against
the restored result.

Requires a SECOND disposable PostgreSQL instance beyond the one
`tests/integration/README.md` already documents, reachable via
`DRILL_TARGET_*` (never `DATABASE_URL`/`MIGRATIONS_DATABASE_URL`/
`PGPASSWORD` -- see `scripts/db_recovery_drill.py`'s own module docstring
for why). Skipped, not failed, when that second instance's configuration is
absent -- so running the existing documented `pytest -m integration`
against only the one instance `tests/integration/README.md` sets up
continues to pass exactly as it did before this phase; only
`.github/workflows/ci.yml`'s dedicated `recovery-drill` job (which stands up
both instances) actually exercises this test.

```bash
# In addition to tests/integration/README.md's own SOURCE instance/env:
docker run -d --rm --name voiceagent-test-pg-target \\
    -e POSTGRES_USER=saas_os -e POSTGRES_PASSWORD=devpassword \\
    -e POSTGRES_DB=voiceagent -p 15433:5432 postgres:16-alpine
docker exec voiceagent-test-pg-target psql -U saas_os -d voiceagent -c "
    CREATE ROLE saas_os_app LOGIN PASSWORD 'devpassword'
        NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
    GRANT CONNECT ON DATABASE voiceagent TO saas_os_app;
"
export DRILL_MODE=true
export DRILL_TARGET_HOST=127.0.0.1
export DRILL_TARGET_PORT=15433
export DRILL_TARGET_DB=voiceagent
export DRILL_TARGET_OWNER_USER=saas_os
export DRILL_TARGET_OWNER_PASSWORD=devpassword
export DRILL_TARGET_APP_USER=saas_os_app
export DRILL_TARGET_APP_PASSWORD=devpassword

pytest -m integration tests/integration/test_recovery_drill_integration.py
```
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import db_recovery_drill as drill  # noqa: E402
import offhost_backup  # noqa: E402

pytestmark = pytest.mark.integration

_REQUIRED_DRILL_VARS = (
    "DRILL_TARGET_HOST",
    "DRILL_TARGET_PORT",
    "DRILL_TARGET_DB",
    "DRILL_TARGET_OWNER_USER",
    "DRILL_TARGET_OWNER_PASSWORD",
    "DRILL_TARGET_APP_USER",
    "DRILL_TARGET_APP_PASSWORD",
)


def _skip_unless_drill_target_configured() -> None:
    missing = [key for key in _REQUIRED_DRILL_VARS if not os.environ.get(key)]
    if missing:
        pytest.skip(
            "DRILL_TARGET_* not configured (missing: "
            f"{missing}) -- this repository's own instance is the SOURCE, "
            "not the drill target; see this module's own docstring for how "
            "to start a second disposable instance and run this test"
        )


def test_recovery_drill_passes_against_a_real_restored_target(tmp_path: Path) -> None:
    _skip_unless_drill_target_configured()

    repo_root = Path(__file__).resolve().parents[2]
    scripts_dir = repo_root / "scripts"
    backup_dir = tmp_path / "backup"
    offhost_dir = tmp_path / "offhost"
    state_dir = tmp_path / "state"
    for directory in (backup_dir, offhost_dir, state_dir):
        directory.mkdir()

    source_url = urlsplit(os.environ["MIGRATIONS_DATABASE_URL"])
    source_host = source_url.hostname or "127.0.0.1"
    source_port = str(source_url.port or 5432)
    source_owner_user = source_url.username or "saas_os"
    source_owner_password = source_url.password or ""

    offhost_env = {
        "PATH": os.environ.get("PATH", ""),
        "PGPASSWORD": source_owner_password,
        "OFFHOST_BACKUP_ENABLED": "true",
        "OFFHOST_BACKUP_DESTINATION_KIND": "command",
        "OFFHOST_BACKUP_DESTINATION_ROOT": str(offhost_dir),
        "OFFHOST_BACKUP_ALLOW_LOCAL_DESTINATION_FOR_TESTING": "true",
        "OFFHOST_BACKUP_TRANSFER_COMMAND": '["cp", "{LOCAL_PATH}", "{REMOTE_ROOT}/{REMOTE_NAME}"]',
        "OFFHOST_BACKUP_VERIFY_COMMAND": '["sha256sum", "{REMOTE_ROOT}/{REMOTE_NAME}"]',
        "OFFHOST_BACKUP_STATE_DIR": str(state_dir),
    }
    # The schema-owning role for the SOURCE, same convention
    # `tests/integration/README.md` already uses (`saas_os`, never the
    # restricted `saas_os_app` -- a backup needs DDL/ownership visibility).
    backup_proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [
            sys.executable,
            str(scripts_dir / "offhost_backup.py"),
            "backup",
            "--host",
            source_host,
            "--port",
            source_port,
            "--db",
            "voiceagent",
            "--user",
            source_owner_user,
            "--output-dir",
            str(backup_dir),
        ],
        env=offhost_env,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert backup_proc.returncode == 0, (
        f"offhost_backup.py backup failed: stdout={backup_proc.stdout!r} "
        f"stderr={backup_proc.stderr!r}"
    )

    dumps = list(backup_dir.glob("*.dump"))
    assert len(dumps) == 1, f"expected exactly one backup artifact, found: {dumps}"
    dump_path = dumps[0]
    meta_path = dump_path.with_name(dump_path.name + ".meta.json")
    manifest_path = dump_path.with_name(dump_path.name + ".manifest.json")
    manifest_checksum_path = manifest_path.with_name(manifest_path.name + ".sha256")

    config = drill.load_config(os.environ.copy())
    report = drill.run_drill(
        config,
        dump_path=dump_path,
        meta_path=meta_path,
        manifest_path=manifest_path,
        manifest_checksum_path=manifest_checksum_path,
        scripts_dir=scripts_dir,
        repo_root=repo_root,
    )

    assert report["verdict"] == "PASS", report
    assert report["steps"]["artifact_verification"]["status"] == "passed"
    assert report["steps"]["target_safety_precondition"]["status"] == "passed"
    assert report["steps"]["restore"]["status"] == "passed"
    assert report["steps"]["migration_check"]["status"] == "passed"
    assert report["steps"]["schema_integrity_check"]["status"] == "passed"
    assert report["steps"]["tenant_isolation_check"]["status"] == "passed"
    assert offhost_backup.MANIFEST_VERSION  # imported module actually used (manifest format)
