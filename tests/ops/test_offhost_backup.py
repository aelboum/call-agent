"""Phase 2.42: hermetic tests for `scripts/offhost_backup.py`.

Config validation, retention selection, manifest secret-redaction, and lock
behaviour are pure logic over local files/dicts -- no PostgreSQL and no real
transport needed to prove them correct, same reasoning as
`tests/ops/test_backup_scripts.py`'s own docstring. Where the real transport
matters (did an actual off-host copy really happen, byte-for-byte), that is
real-infrastructure validation, documented in
`docs/PHASE-2.42-OFFHOST-BACKUP-RETENTION.md`, not repeated here with a fake
standing in for "remote".
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import offhost_backup as m  # noqa: E402

# --------------------------------------------------------------------- #
# Config: disabled (default) mode
# --------------------------------------------------------------------- #


def test_config_defaults_to_offhost_disabled() -> None:
    config = m.load_config({})
    assert config.offhost_enabled is False
    assert config.retention_enabled is False


def test_config_rejects_retention_without_offhost() -> None:
    with pytest.raises(m.ConfigError, match="OFFHOST_BACKUP_ENABLED=true"):
        m.load_config({"OFFHOST_BACKUP_RETENTION_ENABLED": "true"})


# --------------------------------------------------------------------- #
# Config: enabled mode -- fail-closed cases
# --------------------------------------------------------------------- #


def _base_env(**overrides: str) -> dict[str, str]:
    env = {
        "OFFHOST_BACKUP_ENABLED": "true",
        "OFFHOST_BACKUP_DESTINATION_KIND": "command",
        "OFFHOST_BACKUP_DESTINATION_ROOT": "ssh://backup-host/srv/backups",
        "OFFHOST_BACKUP_TRANSFER_COMMAND": json.dumps(["cp", "{LOCAL_PATH}", "{REMOTE_NAME}"]),
        "OFFHOST_BACKUP_VERIFY_COMMAND": json.dumps(["sha256sum", "{REMOTE_NAME}"]),
        "OFFHOST_BACKUP_STATE_DIR": "/srv/offhost-state",
    }
    env.update(overrides)
    return env


def test_config_enabled_minimal_is_valid() -> None:
    config = m.load_config(_base_env())
    assert config.offhost_enabled is True
    assert config.retention_enabled is False
    assert config.destination_root == "ssh://backup-host/srv/backups"
    assert config.transfer_command == ("cp", "{LOCAL_PATH}", "{REMOTE_NAME}")


def test_config_requires_destination_kind_command() -> None:
    with pytest.raises(m.ConfigError, match="DESTINATION_KIND"):
        m.load_config(_base_env(OFFHOST_BACKUP_DESTINATION_KIND="s3-native"))


def test_config_requires_destination_root() -> None:
    env = _base_env()
    del env["OFFHOST_BACKUP_DESTINATION_ROOT"]
    with pytest.raises(m.ConfigError, match="DESTINATION_ROOT"):
        m.load_config(env)


def test_config_rejects_plain_local_destination_without_explicit_opt_in() -> None:
    with pytest.raises(m.ConfigError, match="never off-host"):
        m.load_config(_base_env(OFFHOST_BACKUP_DESTINATION_ROOT="/var/backups/voiceagent"))


def test_config_allows_local_destination_with_explicit_testing_flag() -> None:
    config = m.load_config(
        _base_env(
            OFFHOST_BACKUP_DESTINATION_ROOT="/srv/offhost-sim",
            OFFHOST_BACKUP_ALLOW_LOCAL_DESTINATION_FOR_TESTING="true",
        )
    )
    assert config.destination_root == "/srv/offhost-sim"


def test_config_rejects_webroot_looking_destination() -> None:
    with pytest.raises(m.ConfigError, match="web-served"):
        m.load_config(_base_env(OFFHOST_BACKUP_DESTINATION_ROOT="ssh://host/var/www/html/backups"))


def test_config_rejects_control_characters_in_destination_root() -> None:
    with pytest.raises(m.ConfigError, match="control character"):
        m.load_config(_base_env(OFFHOST_BACKUP_DESTINATION_ROOT="ssh://host/path\nrm -rf /"))


def test_config_rejects_control_characters_in_transfer_command() -> None:
    with pytest.raises(m.ConfigError, match="control character"):
        m.load_config(
            _base_env(
                OFFHOST_BACKUP_TRANSFER_COMMAND=json.dumps(["cp", "{LOCAL_PATH}\n; rm -rf /"])
            )
        )


def test_config_requires_transfer_command() -> None:
    env = _base_env()
    del env["OFFHOST_BACKUP_TRANSFER_COMMAND"]
    with pytest.raises(m.ConfigError, match="TRANSFER_COMMAND"):
        m.load_config(env)


def test_config_rejects_non_json_transfer_command() -> None:
    with pytest.raises(m.ConfigError, match="not valid JSON"):
        m.load_config(_base_env(OFFHOST_BACKUP_TRANSFER_COMMAND="cp {LOCAL_PATH} {REMOTE_NAME}"))


def test_config_rejects_empty_array_transfer_command() -> None:
    with pytest.raises(m.ConfigError, match="one or more strings"):
        m.load_config(_base_env(OFFHOST_BACKUP_TRANSFER_COMMAND="[]"))


def test_config_requires_state_dir() -> None:
    env = _base_env()
    del env["OFFHOST_BACKUP_STATE_DIR"]
    with pytest.raises(m.ConfigError, match="STATE_DIR"):
        m.load_config(env)


def test_config_retention_requires_delete_command_and_policy() -> None:
    with pytest.raises(m.ConfigError, match="DELETE_COMMAND"):
        m.load_config(_base_env(OFFHOST_BACKUP_RETENTION_ENABLED="true"))


def test_config_retention_requires_keep_count_or_keep_days() -> None:
    with pytest.raises(m.ConfigError, match="KEEP_COUNT"):
        m.load_config(
            _base_env(
                OFFHOST_BACKUP_RETENTION_ENABLED="true",
                OFFHOST_BACKUP_DELETE_COMMAND=json.dumps(["rm", "{REMOTE_NAME}"]),
            )
        )


def test_config_retention_valid_with_keep_count() -> None:
    config = m.load_config(
        _base_env(
            OFFHOST_BACKUP_RETENTION_ENABLED="true",
            OFFHOST_BACKUP_DELETE_COMMAND=json.dumps(["rm", "{REMOTE_NAME}"]),
            OFFHOST_BACKUP_RETENTION_KEEP_COUNT="5",
        )
    )
    assert config.retention_enabled is True
    assert config.retention_keep_count == 5


def test_config_rejects_zero_retention_keep_count() -> None:
    with pytest.raises(m.ConfigError, match=">= 1"):
        m.load_config(
            _base_env(
                OFFHOST_BACKUP_RETENTION_ENABLED="true",
                OFFHOST_BACKUP_DELETE_COMMAND=json.dumps(["rm", "{REMOTE_NAME}"]),
                OFFHOST_BACKUP_RETENTION_KEEP_COUNT="0",
            )
        )


def test_config_denylist_cannot_be_passed_through_to_transport() -> None:
    with pytest.raises(m.ConfigError, match="PGPASSWORD"):
        m.load_config(_base_env(OFFHOST_TRANSPORT_ENV_PASSTHROUGH="PGPASSWORD,HOME"))


def test_config_allows_non_credential_passthrough() -> None:
    config = m.load_config(
        _base_env(OFFHOST_TRANSPORT_ENV_PASSTHROUGH="RCLONE_CONFIG, AWS_PROFILE")
    )
    assert config.transport_env_passthrough == ("RCLONE_CONFIG", "AWS_PROFILE")


def test_transport_env_never_contains_db_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPASSWORD", "super-secret")  # pragma: allowlist secret
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")  # pragma: allowlist secret
    monkeypatch.setenv("AWS_PROFILE", "backups")
    config = m.load_config(_base_env(OFFHOST_TRANSPORT_ENV_PASSTHROUGH="AWS_PROFILE"))
    env = m._transport_env(config)
    assert "PGPASSWORD" not in env
    assert "DATABASE_URL" not in env
    assert env.get("AWS_PROFILE") == "backups"


# --------------------------------------------------------------------- #
# Manifest: never secret-bearing
# --------------------------------------------------------------------- #


def test_manifest_has_no_secrets_accepts_clean_manifest() -> None:
    manifest = {
        "backup_format_version": m.MANIFEST_VERSION,
        "backup_id": "voiceagent-20261008T000000Z",
        "created_at_utc": "20261008T000000Z",
        "database": "voiceagent",
        "dump_filename": "voiceagent-20261008T000000Z.dump",
        "sha256": "a" * 64,
        "size_bytes": 123,
        "format": "custom (pg_dump -Fc)",
        "migration_revision": "0012_inbound_routing",
    }
    m.assert_manifest_has_no_secrets(manifest)  # must not raise


@pytest.mark.parametrize(
    "bad_key", ["password", "db_password", "api_token", "secret_value", "client_secret"]
)
def test_manifest_has_no_secrets_rejects_secret_like_key(bad_key: str) -> None:
    with pytest.raises(m.OffHostError, match="secret-bearing"):
        m.assert_manifest_has_no_secrets({bad_key: "whatever"})


def test_manifest_has_no_secrets_rejects_embedded_connection_string() -> None:
    with pytest.raises(m.OffHostError, match="connection string"):
        m.assert_manifest_has_no_secrets(
            {"database": "postgresql://saas_os:hunter2@db-host/voiceagent"}
        )


def test_manifest_written_to_disk_has_no_secret_field(tmp_path: Path) -> None:
    dump_path = tmp_path / "voiceagent-20261008T000000Z.dump"
    dump_path.write_bytes(b"not a real dump")
    meta_path = dump_path.with_name(dump_path.name + ".meta.json")
    meta = {
        "database": "voiceagent",
        "timestamp_utc": "20261008T000000Z",
        "format": "custom (pg_dump -Fc)",
        "size_bytes": dump_path.stat().st_size,
        "sha256": "b" * 64,
        "postgres_server_version": "16.15",
        "pg_dump_version": "pg_dump (PostgreSQL) 16.15",
    }
    meta_path.write_text(json.dumps(meta))
    local = m.LocalBackup(dump_path=dump_path, meta_path=meta_path, meta=meta)

    manifest = m.build_manifest(local, database="voiceagent", migration_revision="0012")
    manifest_path, checksum_path = m.write_manifest(local, manifest)

    written = json.loads(manifest_path.read_text())
    assert "password" not in json.dumps(written).lower()
    assert "secret" not in json.dumps(written).lower()
    digest = checksum_path.read_text().split()[0]
    import hashlib

    assert digest == hashlib.sha256(manifest_path.read_bytes()).hexdigest()


# --------------------------------------------------------------------- #
# Retention: pure selection logic
# --------------------------------------------------------------------- #


def _entry(backup_id: str, created_at_utc: str) -> dict:
    return {
        "backup_id": backup_id,
        "created_at_utc": created_at_utc,
        "dump_name": f"{backup_id}.dump",
        "meta_name": f"{backup_id}.dump.meta.json",
        "manifest_name": f"{backup_id}.dump.manifest.json",
        "manifest_sha256_name": f"{backup_id}.dump.manifest.json.sha256",
        "sha256": "c" * 64,
    }


_NOW = datetime(2026, 10, 8, tzinfo=UTC)


def test_retention_keeps_configured_count() -> None:
    entries = [
        _entry("voiceagent-20261001T000000Z", "20261001T000000Z"),
        _entry("voiceagent-20261002T000000Z", "20261002T000000Z"),
        _entry("voiceagent-20261003T000000Z", "20261003T000000Z"),
        _entry("voiceagent-20261004T000000Z", "20261004T000000Z"),
    ]
    survivors, doomed = m.select_retention(entries, keep_count=2, keep_days=None, now=_NOW)
    assert [e["backup_id"] for e in survivors] == [
        "voiceagent-20261004T000000Z",
        "voiceagent-20261003T000000Z",
    ]
    assert [e["backup_id"] for e in doomed] == [
        "voiceagent-20261002T000000Z",
        "voiceagent-20261001T000000Z",
    ]


def test_retention_never_deletes_newest_even_with_keep_count_zero_effective() -> None:
    entries = [_entry("voiceagent-20261001T000000Z", "20261001T000000Z")]
    survivors, doomed = m.select_retention(entries, keep_count=1, keep_days=None, now=_NOW)
    assert survivors == entries
    assert doomed == []


def test_retention_keep_days_policy() -> None:
    entries = [
        _entry("voiceagent-20260901T000000Z", "20260901T000000Z"),  # > 30 days old
        _entry("voiceagent-20261001T000000Z", "20261001T000000Z"),  # 7 days old
        _entry("voiceagent-20261007T000000Z", "20261007T000000Z"),  # 1 day old
    ]
    survivors, doomed = m.select_retention(entries, keep_count=None, keep_days=10, now=_NOW)
    survivor_ids = {e["backup_id"] for e in survivors}
    assert survivor_ids == {"voiceagent-20261001T000000Z", "voiceagent-20261007T000000Z"}
    assert [e["backup_id"] for e in doomed] == ["voiceagent-20260901T000000Z"]


def test_retention_union_of_count_and_days() -> None:
    entries = [
        _entry("voiceagent-20260801T000000Z", "20260801T000000Z"),
        _entry("voiceagent-20261001T000000Z", "20261001T000000Z"),
        _entry("voiceagent-20261007T000000Z", "20261007T000000Z"),
    ]
    # keep_count=1 alone would only keep the newest; keep_days=10 also saves 10/01.
    survivors, _doomed = m.select_retention(entries, keep_count=1, keep_days=10, now=_NOW)
    survivor_ids = {e["backup_id"] for e in survivors}
    assert survivor_ids == {"voiceagent-20261001T000000Z", "voiceagent-20261007T000000Z"}


def test_retention_never_empties_the_ledger() -> None:
    entries = [_entry("voiceagent-20200101T000000Z", "20200101T000000Z")]
    survivors, doomed = m.select_retention(entries, keep_count=None, keep_days=1, now=_NOW)
    assert len(survivors) == 1
    assert doomed == []


def test_retention_empty_ledger_is_a_no_op() -> None:
    survivors, doomed = m.select_retention([], keep_count=5, keep_days=None, now=_NOW)
    assert survivors == []
    assert doomed == []


def test_retention_ordering_is_by_backup_id_not_list_order() -> None:
    entries = [
        _entry("voiceagent-20261003T000000Z", "20261003T000000Z"),
        _entry("voiceagent-20261001T000000Z", "20261001T000000Z"),
        _entry("voiceagent-20261002T000000Z", "20261002T000000Z"),
    ]
    survivors, _doomed = m.select_retention(entries, keep_count=1, keep_days=None, now=_NOW)
    assert survivors[0]["backup_id"] == "voiceagent-20261003T000000Z"


def test_run_retention_deletes_only_doomed_and_reports_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = m.load_config(
        _base_env(
            OFFHOST_BACKUP_STATE_DIR=str(tmp_path),
            OFFHOST_BACKUP_RETENTION_ENABLED="true",
            OFFHOST_BACKUP_DELETE_COMMAND=json.dumps(["rm", "{REMOTE_NAME}"]),
            OFFHOST_BACKUP_RETENTION_KEEP_COUNT="1",
        )
    )
    entries = [
        _entry("voiceagent-20261001T000000Z", "20261001T000000Z"),
        _entry("voiceagent-20261002T000000Z", "20261002T000000Z"),
    ]
    m.save_ledger(config, entries)

    deleted_names: list[str] = []

    def fake_delete_remote(_config: m.Config, remote_name: str) -> None:
        if "20261001" in remote_name and remote_name.endswith(".dump"):
            raise m.OffHostError("simulated transport failure")
        deleted_names.append(remote_name)

    monkeypatch.setattr(m, "delete_remote", fake_delete_remote)
    result = m.run_retention(config)

    # The failing entry's own dump delete raised -- that whole backup_id must
    # not be considered deleted, and must still be in the ledger afterwards.
    assert any(f["backup_id"] == "voiceagent-20261001T000000Z" for f in result.failed)
    assert all(e["backup_id"] != "voiceagent-20261001T000000Z" for e in result.deleted)
    remaining = m.load_ledger(config)
    assert any(e["backup_id"] == "voiceagent-20261001T000000Z" for e in remaining)
    assert any(e["backup_id"] == "voiceagent-20261002T000000Z" for e in remaining)


def test_run_retention_requires_enabled() -> None:
    config = m.load_config(_base_env())
    with pytest.raises(m.OffHostError, match="not enabled"):
        m.run_retention(config)


# --------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------- #


def test_lock_blocks_concurrent_acquisition(tmp_path: Path) -> None:
    config = m.load_config(_base_env(OFFHOST_BACKUP_STATE_DIR=str(tmp_path)))
    with m.acquire_lock(config):
        with pytest.raises(m.OffHostError, match="already running"):
            with m.acquire_lock(config):
                pass  # pragma: no cover -- must never be reached


def test_lock_released_after_use_allows_reacquire(tmp_path: Path) -> None:
    config = m.load_config(_base_env(OFFHOST_BACKUP_STATE_DIR=str(tmp_path)))
    with m.acquire_lock(config):
        pass
    with m.acquire_lock(config):
        pass  # must not raise -- the first lock was released


def test_lock_recovers_from_stale_dead_pid(tmp_path: Path) -> None:
    config = m.load_config(_base_env(OFFHOST_BACKUP_STATE_DIR=str(tmp_path)))
    lock_path = m._lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # A PID astronomically unlikely to be alive, with a fresh timestamp --
    # proves staleness is decided by liveness, not merely by age.
    lock_path.write_text(
        json.dumps({"pid": 999999, "token": "dead:old", "started_at_utc": m._utc_now_iso()})
    )
    with m.acquire_lock(config):
        pass  # must not raise -- the dead PID makes the lock stale


def test_lock_recovers_from_stale_old_timestamp(tmp_path: Path) -> None:
    config = m.load_config(
        _base_env(OFFHOST_BACKUP_STATE_DIR=str(tmp_path), OFFHOST_BACKUP_LOCK_STALE_SECONDS="1")
    )
    lock_path = m._lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    old_timestamp = "2020-01-01T00:00:00+00:00"
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "token": "self:old", "started_at_utc": old_timestamp})
    )
    with m.acquire_lock(config):
        pass  # must not raise -- the timestamp is far older than the 1s threshold


def test_lock_does_not_remove_a_lock_it_does_not_own(tmp_path: Path) -> None:
    config = m.load_config(_base_env(OFFHOST_BACKUP_STATE_DIR=str(tmp_path)))
    lock_path = m._lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": 999999, "token": "dead:old", "started_at_utc": m._utc_now_iso()})
    )
    gen = m.acquire_lock(config)
    gen.__enter__()
    # Simulate another process recovering the same stale lock and writing its
    # own fresh one while we still believe we hold it.
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "token": "other:fresh", "started_at_utc": m._utc_now_iso()})
    )
    gen.__exit__(None, None, None)
    # Our release must not have deleted the other holder's lock.
    assert lock_path.exists()
    current = json.loads(lock_path.read_text())
    assert current["token"] == "other:fresh"  # noqa: S105 -- lock token, not a password


def test_lock_file_has_restrictive_permissions(tmp_path: Path) -> None:
    config = m.load_config(_base_env(OFFHOST_BACKUP_STATE_DIR=str(tmp_path)))
    with m.acquire_lock(config):
        lock_path = m._lock_path(config)
        mode = lock_path.stat().st_mode & 0o777
        # Windows has no POSIX permission bits to observe -- only assert on
        # platforms where chmod/open-mode actually constrains access, same
        # as tests/ops/test_backup_scripts.py's own convention.
        if os.name == "posix":
            assert mode == 0o600


# --------------------------------------------------------------------- #
# Ledger persistence
# --------------------------------------------------------------------- #


def test_ledger_round_trips_and_has_restrictive_permissions(tmp_path: Path) -> None:
    config = m.load_config(_base_env(OFFHOST_BACKUP_STATE_DIR=str(tmp_path)))
    entries = [_entry("voiceagent-20261008T000000Z", "20261008T000000Z")]
    m.save_ledger(config, entries)
    assert m.load_ledger(config) == entries
    if os.name == "posix":
        mode = m._ledger_path(config).stat().st_mode & 0o777
        assert mode == 0o600


def test_ledger_missing_file_is_empty_list(tmp_path: Path) -> None:
    config = m.load_config(_base_env(OFFHOST_BACKUP_STATE_DIR=str(tmp_path)))
    assert m.load_ledger(config) == []
