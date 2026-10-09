#!/usr/bin/env python
"""Phase 2.42: off-host transfer, retention, and locking around Phase 2.37's
`db_backup.sh`/`db_restore.sh`.

Phase 2.37 proved the local backup/restore *mechanism* (deterministic
`pg_dump -Fc`, checksum, fail-closed integrity checks) but deliberately left
the artifact on the PostgreSQL host with no off-host copy, no retention, and
no scheduling (see that phase's own doc, section 12/14). Phase 2.36 named
exactly that as the remaining production gap.

This script does NOT replace `db_backup.sh`/`db_restore.sh` -- it wraps them.
`db_backup.sh` remains the only thing that ever runs `pg_dump`; this script's
job starts after a local artifact already exists: build a non-secret
manifest, transfer the artifact off-host through an operator-configured
*generic command* transport (never a hardcoded cloud SDK -- see
`docs/PHASE-2.42-OFFHOST-BACKUP-RETENTION.md`), verify its checksum on the
remote side, record it in a local ledger of *verified* backups, and run
deterministic retention against that ledger.

"PostgreSQL backup success means the backup is locally verified AND off-host
verified." -- an off-host transfer failure or checksum mismatch is always a
failed operation, never a degraded success; the local verified artifact is
always retained until an off-host copy is independently proven good.

Never imported by anything under `voiceagent/` -- operational tooling only,
same category as `db_backup.sh`/`db_restore.sh` themselves. Never prints a
secret: `PGPASSWORD`/`DATABASE_URL`/`MIGRATIONS_DATABASE_URL` reach only
`db_backup.sh`'s/`db_restore.sh`'s own subprocess environment, never a
transport command's.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

MANIFEST_VERSION = "voiceagent-offhost-backup-manifest/1"

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

# Rule: a transport subprocess must never be able to read database
# credentials, no matter what an operator passes through. This denylist
# cannot be overridden by OFFHOST_TRANSPORT_ENV_PASSTHROUGH.
_CREDENTIAL_ENV_DENYLIST = {"PGPASSWORD", "DATABASE_URL", "MIGRATIONS_DATABASE_URL"}

# Best-effort guard against an operator accidentally pointing the off-host
# destination root at a web-served directory (rule: artifacts must never
# become publicly readable through an accidentally configured web root).
# Not exhaustive -- a generic command transport can point anywhere -- but
# catches the common accident.
_WEBROOT_HINTS = ("/var/www", "/public/", "wwwroot", "htdocs", "nginx/html", "/static/")

_SECRET_LIKE_MANIFEST_KEYS = ("password", "secret", "token", "credential", "connection_string")


class ConfigError(Exception):
    """Fail-closed configuration problem -- raised before any destructive or
    network action is taken."""


class OffHostError(Exception):
    """An off-host operation (transfer, verification, retention, lock) failed
    after the local backup itself already succeeded."""


# --------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Config:
    offhost_enabled: bool
    retention_enabled: bool
    destination_root: str | None
    transfer_command: tuple[str, ...] | None
    verify_command: tuple[str, ...] | None
    list_command: tuple[str, ...] | None
    delete_command: tuple[str, ...] | None
    fetch_command: tuple[str, ...] | None
    retention_keep_count: int | None
    retention_keep_days: int | None
    state_dir: Path | None
    lock_stale_seconds: float
    transport_env_passthrough: tuple[str, ...]


def _no_control_chars(name: str, value: str) -> str:
    if _CONTROL_CHAR_RE.search(value):
        raise ConfigError(f"{name} contains a control character or newline; rejected")
    return value


def _flag(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise ConfigError(f"{key} must be true/false, got: {raw!r}")


def _parse_command_json(name: str, raw: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{name} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list) or not parsed or not all(isinstance(p, str) for p in parsed):
        raise ConfigError(f"{name} must be a JSON array of one or more strings")
    for token in parsed:
        _no_control_chars(name, token)
    return tuple(parsed)


def _parse_positive_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer: {raw!r}") from exc
    if value < 1:
        raise ConfigError(f"{name} must be >= 1")
    return value


def load_config(env: dict[str, str]) -> Config:
    """Load and validate Phase 2.42 configuration. Fails closed: any
    incomplete/ambiguous/invalid configuration raises `ConfigError` rather
    than silently falling back to a weaker mode."""
    offhost_enabled = _flag(env, "OFFHOST_BACKUP_ENABLED", False)
    retention_enabled = _flag(env, "OFFHOST_BACKUP_RETENTION_ENABLED", False)

    if not offhost_enabled:
        if retention_enabled:
            raise ConfigError(
                "OFFHOST_BACKUP_RETENTION_ENABLED=true requires OFFHOST_BACKUP_ENABLED=true "
                "-- retention has nothing to retain without off-host transfer"
            )
        return Config(
            offhost_enabled=False,
            retention_enabled=False,
            destination_root=None,
            transfer_command=None,
            verify_command=None,
            list_command=None,
            delete_command=None,
            fetch_command=None,
            retention_keep_count=None,
            retention_keep_days=None,
            state_dir=None,
            lock_stale_seconds=3600.0,
            transport_env_passthrough=(),
        )

    destination_kind = env.get("OFFHOST_BACKUP_DESTINATION_KIND", "").strip()
    if destination_kind != "command":
        raise ConfigError(
            "OFFHOST_BACKUP_DESTINATION_KIND must be 'command' -- the one "
            "provider-neutral transport this script supports (an "
            f"operator-configured command, never a cloud SDK); got {destination_kind!r}"
        )

    destination_root = env.get("OFFHOST_BACKUP_DESTINATION_ROOT", "").strip()
    if not destination_root:
        raise ConfigError("OFFHOST_BACKUP_DESTINATION_ROOT is required when off-host is enabled")
    _no_control_chars("OFFHOST_BACKUP_DESTINATION_ROOT", destination_root)

    allow_local = _flag(env, "OFFHOST_BACKUP_ALLOW_LOCAL_DESTINATION_FOR_TESTING", False)
    looks_external = "://" in destination_root
    if not looks_external and not allow_local:
        raise ConfigError(
            "OFFHOST_BACKUP_DESTINATION_ROOT does not look like a genuinely "
            "external destination (expected a '<scheme>://' root, e.g. "
            "s3://bucket/prefix or ssh://host/path). A plain local directory "
            "is never off-host; set "
            "OFFHOST_BACKUP_ALLOW_LOCAL_DESTINATION_FOR_TESTING=true only for "
            "test/integration use, never in production."
        )
    lowered_root = destination_root.lower()
    if any(hint in lowered_root for hint in _WEBROOT_HINTS):
        raise ConfigError(
            "OFFHOST_BACKUP_DESTINATION_ROOT looks like a web-served "
            "directory; refusing to risk publishing backup artifacts over HTTP"
        )

    transfer_raw = env.get("OFFHOST_BACKUP_TRANSFER_COMMAND", "")
    if not transfer_raw:
        raise ConfigError("OFFHOST_BACKUP_TRANSFER_COMMAND is required when off-host is enabled")
    transfer_command = _parse_command_json("OFFHOST_BACKUP_TRANSFER_COMMAND", transfer_raw)

    verify_raw = env.get("OFFHOST_BACKUP_VERIFY_COMMAND", "")
    if not verify_raw:
        raise ConfigError("OFFHOST_BACKUP_VERIFY_COMMAND is required when off-host is enabled")
    verify_command = _parse_command_json("OFFHOST_BACKUP_VERIFY_COMMAND", verify_raw)

    list_raw = env.get("OFFHOST_BACKUP_LIST_COMMAND", "")
    list_command = (
        _parse_command_json("OFFHOST_BACKUP_LIST_COMMAND", list_raw) if list_raw else None
    )

    fetch_raw = env.get("OFFHOST_BACKUP_FETCH_COMMAND", "")
    fetch_command = (
        _parse_command_json("OFFHOST_BACKUP_FETCH_COMMAND", fetch_raw) if fetch_raw else None
    )

    state_dir_raw = env.get("OFFHOST_BACKUP_STATE_DIR", "").strip()
    if not state_dir_raw:
        raise ConfigError("OFFHOST_BACKUP_STATE_DIR is required when off-host is enabled")
    _no_control_chars("OFFHOST_BACKUP_STATE_DIR", state_dir_raw)
    state_dir = Path(state_dir_raw)

    delete_command = None
    retention_keep_count = None
    retention_keep_days = None
    if retention_enabled:
        delete_raw = env.get("OFFHOST_BACKUP_DELETE_COMMAND", "")
        if not delete_raw:
            raise ConfigError("OFFHOST_BACKUP_DELETE_COMMAND is required when retention is enabled")
        delete_command = _parse_command_json("OFFHOST_BACKUP_DELETE_COMMAND", delete_raw)

        count_raw = env.get("OFFHOST_BACKUP_RETENTION_KEEP_COUNT", "").strip()
        days_raw = env.get("OFFHOST_BACKUP_RETENTION_KEEP_DAYS", "").strip()
        if not count_raw and not days_raw:
            raise ConfigError(
                "at least one of OFFHOST_BACKUP_RETENTION_KEEP_COUNT / "
                "OFFHOST_BACKUP_RETENTION_KEEP_DAYS is required when retention is enabled"
            )
        if count_raw:
            retention_keep_count = _parse_positive_int(
                "OFFHOST_BACKUP_RETENTION_KEEP_COUNT", count_raw
            )
        if days_raw:
            retention_keep_days = _parse_positive_int(
                "OFFHOST_BACKUP_RETENTION_KEEP_DAYS", days_raw
            )

    lock_stale_raw = env.get("OFFHOST_BACKUP_LOCK_STALE_SECONDS", "3600").strip()
    try:
        lock_stale_seconds = float(lock_stale_raw)
    except ValueError as exc:
        raise ConfigError(
            f"OFFHOST_BACKUP_LOCK_STALE_SECONDS must be a number: {lock_stale_raw!r}"
        ) from exc
    if lock_stale_seconds <= 0:
        raise ConfigError("OFFHOST_BACKUP_LOCK_STALE_SECONDS must be > 0")

    passthrough_raw = env.get("OFFHOST_TRANSPORT_ENV_PASSTHROUGH", "").strip()
    passthrough = tuple(p.strip() for p in passthrough_raw.split(",") if p.strip())
    denied = sorted(set(passthrough) & _CREDENTIAL_ENV_DENYLIST)
    if denied:
        raise ConfigError(
            f"OFFHOST_TRANSPORT_ENV_PASSTHROUGH may never include {denied} -- "
            "database credentials must never reach a transport subprocess"
        )

    return Config(
        offhost_enabled=True,
        retention_enabled=retention_enabled,
        destination_root=destination_root,
        transfer_command=transfer_command,
        verify_command=verify_command,
        list_command=list_command,
        delete_command=delete_command,
        fetch_command=fetch_command,
        retention_keep_count=retention_keep_count,
        retention_keep_days=retention_keep_days,
        state_dir=state_dir,
        lock_stale_seconds=lock_stale_seconds,
        transport_env_passthrough=passthrough,
    )


# --------------------------------------------------------------------- #
# Transport (generic command boundary -- never a cloud SDK)
# --------------------------------------------------------------------- #


def _substitute(command: tuple[str, ...], **tokens: str) -> list[str]:
    out = []
    for arg in command:
        for key, value in tokens.items():
            arg = arg.replace("{" + key + "}", value)
        out.append(arg)
    return out


def _transport_env(config: Config) -> dict[str, str]:
    """Minimal, explicitly-allowlisted environment for a transport
    subprocess. Never inherits the full process environment -- PGPASSWORD/
    DATABASE_URL/MIGRATIONS_DATABASE_URL can never reach a destination
    command, even if an operator lists them in the passthrough allowlist."""
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "SystemRoot", "SystemDrive", "TEMP", "TMP", "USERPROFILE"):
        if key in os.environ:
            env[key] = os.environ[key]
    for key in config.transport_env_passthrough:
        if key in _CREDENTIAL_ENV_DENYLIST:
            continue
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _run_transport(
    command: list[str], config: Config, *, timeout: float = 300.0
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 -- argument array (no shell), sanitized env, operator-configured destination
            command,
            env=_transport_env(config),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OffHostError(f"transport command not found: {command[0]!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise OffHostError(f"transport command timed out after {timeout}s: {command}") from exc


def _require[T](value: T | None, what: str) -> T:
    """Narrows an Optional config field that a caller has already guaranteed
    is set (by construction, not by chance) into a concrete value -- raising
    a clear internal error instead of an `AssertionError` if that invariant
    is ever violated."""
    if value is None:
        raise OffHostError(f"{what} is not configured")
    return value


def transfer_artifact(config: Config, local_path: Path) -> None:
    transfer_command = _require(config.transfer_command, "OFFHOST_BACKUP_TRANSFER_COMMAND")
    command = _substitute(
        transfer_command,
        LOCAL_PATH=str(local_path),
        REMOTE_NAME=local_path.name,
        REMOTE_ROOT=config.destination_root or "",
    )
    result = _run_transport(command, config)
    if result.returncode != 0:
        raise OffHostError(
            f"off-host transfer failed for {local_path.name} (exit {result.returncode}): "
            f"{result.stderr.strip()[:500]}"
        )


def remote_sha256(config: Config, remote_name: str) -> str:
    verify_command = _require(config.verify_command, "OFFHOST_BACKUP_VERIFY_COMMAND")
    command = _substitute(
        verify_command, REMOTE_NAME=remote_name, REMOTE_ROOT=config.destination_root or ""
    )
    result = _run_transport(command, config)
    if result.returncode != 0:
        raise OffHostError(
            f"remote checksum command failed for {remote_name} (exit {result.returncode}): "
            f"{result.stderr.strip()[:500]}"
        )
    match = _SHA256_RE.search(result.stdout)
    if not match:
        raise OffHostError(
            f"remote checksum command produced no sha256 for {remote_name}: {result.stdout!r}"
        )
    return match.group(1).lower()


def remote_list(config: Config) -> list[str]:
    list_command = _require(config.list_command, "OFFHOST_BACKUP_LIST_COMMAND")
    command = _substitute(list_command, REMOTE_ROOT=config.destination_root or "")
    result = _run_transport(command, config)
    if result.returncode != 0:
        raise OffHostError(
            f"remote list command failed (exit {result.returncode}): {result.stderr.strip()[:500]}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def remote_exists(config: Config, remote_name: str) -> bool:
    if config.list_command:
        return remote_name in remote_list(config)
    try:
        remote_sha256(config, remote_name)
    except OffHostError:
        return False
    return True


def delete_remote(config: Config, remote_name: str) -> None:
    delete_command = _require(config.delete_command, "OFFHOST_BACKUP_DELETE_COMMAND")
    command = _substitute(
        delete_command, REMOTE_NAME=remote_name, REMOTE_ROOT=config.destination_root or ""
    )
    result = _run_transport(command, config)
    if result.returncode != 0:
        raise OffHostError(
            f"remote delete failed for {remote_name} (exit {result.returncode}): "
            f"{result.stderr.strip()[:500]}"
        )


def fetch_artifact(config: Config, remote_name: str, local_path: Path) -> None:
    if config.fetch_command is None:
        raise OffHostError(
            "OFFHOST_BACKUP_FETCH_COMMAND is not configured -- cannot recover an off-host "
            "artifact without it"
        )
    command = _substitute(
        config.fetch_command,
        REMOTE_NAME=remote_name,
        REMOTE_ROOT=config.destination_root or "",
        LOCAL_PATH=str(local_path),
    )
    result = _run_transport(command, config)
    if result.returncode != 0:
        raise OffHostError(
            f"off-host fetch failed for {remote_name} (exit {result.returncode}): "
            f"{result.stderr.strip()[:500]}"
        )


# --------------------------------------------------------------------- #
# Local backup (delegates to db_backup.sh -- never reimplemented here)
# --------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class LocalBackup:
    dump_path: Path
    meta_path: Path
    meta: dict


def run_local_backup(
    *, host: str, port: str, db: str, user: str, output_dir: Path, scripts_dir: Path
) -> LocalBackup:
    """Invokes the unmodified Phase 2.37 `db_backup.sh` as a subprocess
    (argument array, never a shell string) and parses its own printed
    success line to locate the artifact it produced. `PGPASSWORD` flows to
    this one subprocess only -- never to a transport command."""
    script = scripts_dir / "db_backup.sh"
    proc = subprocess.run(  # noqa: S603 -- fixed local script, argument array, no shell
        [  # noqa: S607 -- "bash" resolved via PATH, same fixed invocation as db_backup.sh itself
            "bash",
            str(script),
            "--host",
            host,
            "--port",
            port,
            "--db",
            db,
            "--user",
            user,
            "--output-dir",
            str(output_dir),
        ],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise OffHostError(f"local backup failed: db_backup.sh exited {proc.returncode}")

    dump_path: Path | None = None
    marker = "[backup] OK: "
    for line in proc.stdout.splitlines():
        if line.startswith(marker):
            rest = line[len(marker) :]
            dump_path = Path(rest.split(" (", 1)[0].strip())
            break
    if dump_path is None or not dump_path.exists():
        raise OffHostError(
            "could not determine the local backup artifact path from db_backup.sh's own output"
        )
    meta_path = dump_path.with_name(dump_path.name + ".meta.json")
    if not meta_path.exists():
        raise OffHostError(f"expected sidecar metadata missing: {meta_path}")
    meta = json.loads(meta_path.read_text())
    return LocalBackup(dump_path=dump_path, meta_path=meta_path, meta=meta)


# --------------------------------------------------------------------- #
# Manifest (non-secret metadata, detached-checksum bound to the artifact)
# --------------------------------------------------------------------- #


def assert_manifest_has_no_secrets(manifest: dict) -> None:
    """Defense in depth: even though every field below is built from known
    non-secret sources, refuse to write a manifest whose own field *names*
    look secret-bearing -- a future accidental addition fails closed instead
    of silently shipping a credential off-host."""
    for key, value in manifest.items():
        lowered = key.lower()
        if any(bad in lowered for bad in _SECRET_LIKE_MANIFEST_KEYS):
            raise OffHostError(f"manifest field {key!r} looks secret-bearing; refusing to write it")
        if isinstance(value, str) and ("://" in value and "@" in value.split("://", 1)[1]):
            raise OffHostError(
                f"manifest field {key!r} looks like a connection string with embedded "
                "credentials; refusing to write it"
            )


def build_manifest(local: LocalBackup, *, database: str, migration_revision: str | None) -> dict:
    return {
        "backup_format_version": MANIFEST_VERSION,
        "backup_id": local.dump_path.stem,
        "created_at_utc": local.meta.get("timestamp_utc"),
        "database": database,
        "dump_filename": local.dump_path.name,
        "sha256": local.meta["sha256"],
        "size_bytes": local.meta["size_bytes"],
        "format": local.meta.get("format"),
        "migration_revision": migration_revision,
    }


def write_manifest(local: LocalBackup, manifest: dict) -> tuple[Path, Path]:
    assert_manifest_has_no_secrets(manifest)
    manifest_path = local.dump_path.with_name(local.dump_path.name + ".manifest.json")
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    checksum_path = manifest_path.with_name(manifest_path.name + ".sha256")
    checksum_path.write_text(f"{digest}  {manifest_path.name}\n")
    for path in (local.dump_path, local.meta_path, manifest_path, checksum_path):
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    return manifest_path, checksum_path


# --------------------------------------------------------------------- #
# Off-host verification
# --------------------------------------------------------------------- #


def off_host_verify(
    config: Config, local: LocalBackup, manifest_path: Path, checksum_path: Path
) -> None:
    """Transfers the dump plus its three sidecars, then proves the remote
    dump byte-for-byte: existence check, remote sha256, compare against the
    locally-computed one. Never trusts a zero exit code from the transfer
    command alone."""
    for path in (local.dump_path, local.meta_path, manifest_path, checksum_path):
        transfer_artifact(config, path)

    remote_name = local.dump_path.name
    if not remote_exists(config, remote_name):
        raise OffHostError(f"remote artifact missing immediately after transfer: {remote_name}")

    remote_digest = remote_sha256(config, remote_name)
    expected = local.meta["sha256"]
    if remote_digest != expected:
        raise OffHostError(
            f"remote checksum mismatch for {remote_name}: expected {expected}, "
            f"remote reported {remote_digest}"
        )


# --------------------------------------------------------------------- #
# Ledger of verified backups (retention's only source of truth)
# --------------------------------------------------------------------- #


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ledger_path(config: Config) -> Path:
    state_dir = _require(config.state_dir, "OFFHOST_BACKUP_STATE_DIR")
    return state_dir / "offhost_backup_ledger.json"


def load_ledger(config: Config) -> list[dict]:
    path = _ledger_path(config)
    if not path.exists():
        return []
    return json.loads(path.read_text())


def save_ledger(config: Config, entries: list[dict]) -> None:
    path = _ledger_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def record_verified(config: Config, local: LocalBackup, manifest: dict) -> None:
    """Appends to the ledger ONLY after off-host verification has already
    succeeded -- an entry's mere presence in the ledger is therefore proof
    of verification; retention never needs a separate verified flag."""
    entries = load_ledger(config)
    entries.append(
        {
            "backup_id": manifest["backup_id"],
            "created_at_utc": manifest["created_at_utc"],
            "dump_name": local.dump_path.name,
            "meta_name": local.meta_path.name,
            "manifest_name": local.dump_path.name + ".manifest.json",
            "manifest_sha256_name": local.dump_path.name + ".manifest.json.sha256",
            "sha256": manifest["sha256"],
            "verified_at_utc": _utc_now_iso(),
        }
    )
    save_ledger(config, entries)


# --------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class RetentionResult:
    survivors: list[dict]
    deleted: list[dict]
    failed: list[dict]


def select_retention(
    entries: list[dict],
    *,
    keep_count: int | None,
    keep_days: int | None,
    now: datetime,
) -> tuple[list[dict], list[dict]]:
    """Pure policy decision -- no I/O. Deterministic ordering by `backup_id`
    (which embeds the UTC backup timestamp), never filesystem mtime. The
    newest verified entry always survives, regardless of policy -- retention
    can never empty the ledger."""
    if not entries:
        return [], []
    ordered = sorted(entries, key=lambda e: e["backup_id"], reverse=True)
    survivor_idx: set[int] = set()
    if keep_count is not None:
        survivor_idx.update(range(min(keep_count, len(ordered))))
    if keep_days is not None:
        cutoff = now - timedelta(days=keep_days)
        for i, entry in enumerate(ordered):
            created = datetime.strptime(entry["created_at_utc"], _TIMESTAMP_FORMAT).replace(
                tzinfo=UTC
            )
            if created >= cutoff:
                survivor_idx.add(i)
    survivor_idx.add(0)  # never delete the newest verified backup
    survivors = [ordered[i] for i in sorted(survivor_idx)]
    doomed = [ordered[i] for i in range(len(ordered)) if i not in survivor_idx]
    return survivors, doomed


def run_retention(config: Config) -> RetentionResult:
    if not config.retention_enabled:
        raise OffHostError("OFFHOST_BACKUP_RETENTION_ENABLED is not enabled")
    entries = load_ledger(config)
    survivors, doomed = select_retention(
        entries,
        keep_count=config.retention_keep_count,
        keep_days=config.retention_keep_days,
        now=datetime.now(UTC),
    )
    deleted: list[dict] = []
    failed: list[dict] = []
    for entry in doomed:
        names = [
            entry["dump_name"],
            entry["meta_name"],
            entry["manifest_name"],
            entry["manifest_sha256_name"],
        ]
        entry_ok = True
        for name in names:
            try:
                delete_remote(config, name)
            except OffHostError as exc:
                entry_ok = False
                failed.append({"backup_id": entry["backup_id"], "name": name, "error": str(exc)})
        if entry_ok:
            deleted.append(entry)

    # Never silently drop a failed deletion from the ledger: only remove
    # entries that were actually deleted. A failed entry stays in the
    # ledger (still "verified", still retained remotely) so the next
    # retention run retries it rather than losing track of it.
    deleted_ids = {e["backup_id"] for e in deleted}
    remaining = [e for e in entries if e["backup_id"] not in deleted_ids]
    save_ledger(config, remaining)
    return RetentionResult(survivors=survivors, deleted=deleted, failed=failed)


# --------------------------------------------------------------------- #
# Locking -- robust against stale processes, never permanently deadlocked
# --------------------------------------------------------------------- #


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but owned by someone else -- treat as alive
    except OSError:
        return False
    return True


def _lock_path(config: Config) -> Path:
    state_dir = _require(config.state_dir, "OFFHOST_BACKUP_STATE_DIR")
    return state_dir / "offhost_backup.lock"


@contextlib.contextmanager
def acquire_lock(config: Config):
    """A lock file holding `{pid, token, started_at_utc}`. A second
    concurrent run fails immediately rather than racing `db_backup.sh`
    against itself. A lock is only ever removed by the holder that wrote it
    (checked by `token`) or, on acquire, when the recorded PID is dead or the
    lock is older than `OFFHOST_BACKUP_LOCK_STALE_SECONDS` -- so a crashed
    holder cannot deadlock future backups indefinitely."""
    state_dir = _require(config.state_dir, "OFFHOST_BACKUP_STATE_DIR")
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _lock_path(config)
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    payload = json.dumps({"pid": os.getpid(), "token": token, "started_at_utc": _utc_now_iso()})

    def _try_create() -> bool:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        return True

    if not _try_create():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = None
        stale = existing is None
        if existing is not None:
            started = datetime.fromisoformat(existing["started_at_utc"])
            age_seconds = (datetime.now(UTC) - started).total_seconds()
            pid_dead = not _pid_alive(existing.get("pid", -1))
            stale = pid_dead or age_seconds > config.lock_stale_seconds
        if not stale:
            raise OffHostError(
                "another backup/retention operation is already running (lock held at "
                f"{path}); refusing to run concurrently"
            )
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        if not _try_create():
            raise OffHostError(
                f"lock contended during stale-lock recovery at {path}; try again shortly"
            )

    try:
        yield
    finally:
        try:
            current = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            current = None
        if current is not None and current.get("token") == token:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #


def _cmd_backup(args: argparse.Namespace) -> int:
    try:
        config = load_config(os.environ.copy())
    except ConfigError as exc:
        print(f"ERROR: configuration: {exc}", file=sys.stderr)
        return 2

    scripts_dir = Path(__file__).resolve().parent
    output_dir = Path(args.output_dir)

    if not config.offhost_enabled:
        try:
            local = run_local_backup(
                host=args.host,
                port=args.port,
                db=args.db,
                user=args.user,
                output_dir=output_dir,
                scripts_dir=scripts_dir,
            )
        except OffHostError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(
            f"[offhost-backup] OFFHOST_BACKUP_ENABLED=false -- local-only backup: {local.dump_path}"
        )
        return 0

    try:
        with acquire_lock(config):
            try:
                local = run_local_backup(
                    host=args.host,
                    port=args.port,
                    db=args.db,
                    user=args.user,
                    output_dir=output_dir,
                    scripts_dir=scripts_dir,
                )
            except OffHostError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1

            manifest = build_manifest(
                local, database=args.db, migration_revision=args.migration_revision
            )
            manifest_path, checksum_path = write_manifest(local, manifest)

            try:
                off_host_verify(config, local, manifest_path, checksum_path)
            except OffHostError as exc:
                print(
                    f"ERROR: off-host verification failed -- local verified artifact "
                    f"RETAINED at {local.dump_path}: {exc}",
                    file=sys.stderr,
                )
                return 1

            record_verified(config, local, manifest)
            print(
                f"[offhost-backup] OK: {local.dump_path.name} locally verified AND "
                f"off-host verified at {config.destination_root}"
            )

            if not config.retention_enabled:
                return 0

            result = run_retention(config)
            print(
                f"[offhost-backup] retention: kept {len(result.survivors)}, "
                f"deleted {len(result.deleted)}, failed {len(result.failed)}"
            )
            if result.failed:
                print(f"ERROR: retention cleanup failed for: {result.failed}", file=sys.stderr)
                return 1
            return 0
    except OffHostError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_retention(_args: argparse.Namespace) -> int:
    try:
        config = load_config(os.environ.copy())
    except ConfigError as exc:
        print(f"ERROR: configuration: {exc}", file=sys.stderr)
        return 2
    if not config.retention_enabled:
        print(
            "ERROR: retention is not enabled (OFFHOST_BACKUP_RETENTION_ENABLED=false)",
            file=sys.stderr,
        )
        return 2
    try:
        with acquire_lock(config):
            result = run_retention(config)
    except OffHostError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"[offhost-retention] kept {len(result.survivors)}, deleted {len(result.deleted)}, "
        f"failed {len(result.failed)}"
    )
    return 1 if result.failed else 0


def check_freshness(ledger: list[dict], *, max_age_hours: float, now: datetime) -> dict:
    """Phase 2.44: closes the gap this phase's own §13 named ("a failed
    scheduled backup/retention run must currently be noticed from the
    scheduler's own job-failure signal ... nothing in this phase pages
    anyone"). Reads the existing verified-backup ledger -- no new state, no
    new infrastructure, no scheduler -- and reports whether the newest
    *verified* (off-host-confirmed, §2's own ledger contract) backup is
    recent enough. An empty ledger is always a FAIL: it means no backup has
    ever been off-host-verified, which is strictly worse than one merely
    being stale."""
    if not ledger:
        return {
            "verdict": "FAIL",
            "reason": "ledger is empty -- no backup has ever been off-host verified",
            "backups_in_ledger": 0,
            "max_age_hours": max_age_hours,
        }
    newest = max(ledger, key=lambda entry: entry["verified_at_utc"])
    verified_at = datetime.fromisoformat(newest["verified_at_utc"])
    age_hours = (now - verified_at).total_seconds() / 3600.0
    stale = age_hours > max_age_hours
    return {
        "verdict": "FAIL" if stale else "PASS",
        "reason": (
            f"newest verified backup is {age_hours:.1f}h old, "
            f"exceeding the {max_age_hours}h threshold"
            if stale
            else None
        ),
        "backups_in_ledger": len(ledger),
        "newest_backup_id": newest["backup_id"],
        "newest_verified_at_utc": newest["verified_at_utc"],
        "age_hours": round(age_hours, 2),
        "max_age_hours": max_age_hours,
    }


def _cmd_freshness(args: argparse.Namespace) -> int:
    try:
        config = load_config(os.environ.copy())
    except ConfigError as exc:
        print(f"ERROR: configuration: {exc}", file=sys.stderr)
        return 2
    if not config.offhost_enabled:
        print("ERROR: off-host is not enabled; there is no ledger to check", file=sys.stderr)
        return 2

    ledger = load_ledger(config)
    report = check_freshness(ledger, max_age_hours=args.max_age_hours, now=datetime.now(UTC))
    print(json.dumps(report, sort_keys=True))
    return 0 if report["verdict"] == "PASS" else 1


def _cmd_recover(args: argparse.Namespace) -> int:
    try:
        config = load_config(os.environ.copy())
    except ConfigError as exc:
        print(f"ERROR: configuration: {exc}", file=sys.stderr)
        return 2
    if not config.offhost_enabled:
        print("ERROR: off-host is not enabled; nothing to recover from", file=sys.stderr)
        return 2

    ledger = load_ledger(config)
    entry = None
    if args.backup_id:
        entry = next((e for e in ledger if e["backup_id"] == args.backup_id), None)
        if entry is None:
            print(
                f"ERROR: backup_id {args.backup_id!r} not found in the local ledger",
                file=sys.stderr,
            )
            return 1
    else:
        if not ledger:
            print("ERROR: ledger is empty and no --backup-id was given", file=sys.stderr)
            return 1
        entry = sorted(ledger, key=lambda e: e["backup_id"], reverse=True)[0]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    local_path = output_dir / entry["dump_name"]

    try:
        fetch_artifact(config, entry["dump_name"], local_path)
    except OffHostError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not local_path.exists():
        print(
            f"ERROR: fetch command reported success but {local_path} does not exist",
            file=sys.stderr,
        )
        return 1

    digest = hashlib.sha256(local_path.read_bytes()).hexdigest()
    if digest != entry["sha256"]:
        print(
            f"ERROR: checksum mismatch after recovery: expected {entry['sha256']}, got {digest} "
            f"-- recovered artifact is NOT trusted",
            file=sys.stderr,
        )
        return 1

    print(
        f"[offhost-recover] OK: {entry['backup_id']} recovered and checksum-verified at "
        f"{local_path}"
    )
    print("[offhost-recover] hand this file to scripts/db_restore.sh --input <path> to restore it")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backup_parser = sub.add_parser(
        "backup", help="run a local backup, then (if enabled) off-host transfer + retention"
    )
    backup_parser.add_argument("--host", required=True)
    backup_parser.add_argument("--port", required=True)
    backup_parser.add_argument("--db", required=True)
    backup_parser.add_argument("--user", required=True)
    backup_parser.add_argument("--output-dir", required=True)
    backup_parser.add_argument(
        "--migration-revision",
        default=None,
        help="operator-supplied, non-secret schema/migration revision recorded in the manifest",
    )
    backup_parser.set_defaults(func=_cmd_backup)

    retention_parser = sub.add_parser(
        "retention", help="run retention only, against the existing local ledger (idempotent)"
    )
    retention_parser.set_defaults(func=_cmd_retention)

    freshness_parser = sub.add_parser(
        "freshness",
        help="check whether the newest off-host-verified backup is recent enough "
        "(reads the existing ledger only; writes nothing, deletes nothing)",
    )
    freshness_parser.add_argument(
        "--max-age-hours",
        required=True,
        type=float,
        help="FAIL if the newest verified backup in the ledger is older than this",
    )
    freshness_parser.set_defaults(func=_cmd_freshness)

    recover_parser = sub.add_parser(
        "recover", help="fetch an off-host artifact back to a local path and verify its checksum"
    )
    recover_parser.add_argument("--output-dir", required=True)
    recover_parser.add_argument(
        "--backup-id", default=None, help="defaults to the newest verified backup in the ledger"
    )
    recover_parser.set_defaults(func=_cmd_recover)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
