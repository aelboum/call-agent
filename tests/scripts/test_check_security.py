"""Regression tests for `scripts/check-security.sh`'s detect-secrets gate.

`detect-secrets scan --baseline FILE` is a generator, not a gate: it always
exits 0, and when FILE already exists it silently rewrites it to absorb
every current finding as if reviewed. Running it was the entire previous
contents of `scripts/check-security.sh`, which meant the "security check"
step in CI could never fail on a newly introduced secret -- it would just
merge the secret into the local, never-committed baseline and exit 0.

These tests build disposable, hermetic git repositories (never touching
this repository's own git state) and run the real
`detect_secrets.pre_commit_hook` module -- what the fixed script now
invokes -- to prove the gate actually fails closed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECK_SECURITY_SH = _REPO_ROOT / "scripts" / "check-security.sh"
_REAL_BASELINE = _REPO_ROOT / ".secrets.baseline"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, no user input
        ["git", "-C", str(repo), *args],  # noqa: S607 -- resolved via PATH, same convention as other tests
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    """A disposable git repo, seeded with this project's real plugin/filter
    config (so the gate's actual behaviour is under test, not a stand-in)
    but an empty, already-reviewed `results` baseline."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")

    real_baseline = json.loads(_REAL_BASELINE.read_text())
    baseline = {
        "version": real_baseline["version"],
        "plugins_used": real_baseline["plugins_used"],
        "filters_used": real_baseline["filters_used"],
        "results": {},
        "generated_at": "2026-01-01T00:00:00Z",
    }
    (repo / ".secrets.baseline").write_text(json.dumps(baseline, indent=2) + "\n")
    (repo / "app.py").write_text('def greet() -> str:\n    return "hello world"\n')

    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _run_hook(repo: Path, *extra_files: str) -> subprocess.CompletedProcess[str]:
    tracked = _git(repo, "ls-files").stdout.split()
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, no user input
        [
            sys.executable,
            "-m",
            "detect_secrets.pre_commit_hook",
            "--baseline",
            ".secrets.baseline",
            *tracked,
            *extra_files,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def _run_check_security_sh(repo: Path) -> subprocess.CompletedProcess[str]:
    python_dir = str(Path(sys.executable).parent)
    env = {**os.environ, "PATH": python_dir + os.pathsep + os.environ["PATH"]}
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, no user input
        ["bash", str(_CHECK_SECURITY_SH)],  # noqa: S607 -- resolved via PATH, same convention as other tests
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


# --------------------------------------------------------------------- #
# 1. A clean repository passes.
# --------------------------------------------------------------------- #


def test_clean_repo_passes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    result = _run_hook(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_clean_repo_passes_via_check_security_sh(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    result = _run_check_security_sh(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok: no unreviewed findings" in result.stdout


# --------------------------------------------------------------------- #
# 2. A newly introduced synthetic secret fails the gate, without ever
#    printing the secret's value.
# --------------------------------------------------------------------- #


def test_new_synthetic_secret_fails_closed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    synthetic_secret = "Tr0ub4dor-SyntheticTestOnly-9f8e7d"  # noqa: S105 -- synthetic, test-only
    (repo / "config.py").write_text(
        f'DATABASE_URL = "postgresql://admin:{synthetic_secret}@db.example.com/app"\n',
    )

    result = _run_hook(repo, "config.py")

    assert result.returncode != 0
    combined_output = result.stdout + result.stderr
    assert synthetic_secret not in combined_output, "gate must never print the secret value"
    assert "Basic Auth Credentials" in combined_output
    assert "config.py" in combined_output


def test_new_synthetic_secret_fails_check_security_sh(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    synthetic_secret = "Tr0ub4dor-SyntheticTestOnly-9f8e7d"  # noqa: S105 -- synthetic, test-only
    (repo / "config.py").write_text(
        f'DATABASE_URL = "postgresql://admin:{synthetic_secret}@db.example.com/app"\n',
    )
    _git(repo, "add", "config.py")

    result = _run_check_security_sh(repo)

    assert result.returncode != 0
    combined_output = result.stdout + result.stderr
    assert synthetic_secret not in combined_output
    assert "FAIL" in combined_output


# --------------------------------------------------------------------- #
# 3. A reviewed, correctly baselined finding is accepted, not treated as
#    newly introduced.
# --------------------------------------------------------------------- #


def test_reviewed_baselined_secret_passes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    synthetic_secret = "Tr0ub4dor-SyntheticTestOnly-9f8e7d"  # noqa: S105 -- synthetic, test-only
    (repo / "config.py").write_text(
        f'DATABASE_URL = "postgresql://admin:{synthetic_secret}@db.example.com/app"\n',
    )

    # Confirm it is flagged before being reviewed.
    assert _run_hook(repo, "config.py").returncode != 0

    scan = subprocess.run(  # noqa: S603 -- fixed argv, no shell, no user input
        [
            sys.executable,
            "-m",
            "detect_secrets",
            "scan",
            "--baseline",
            ".secrets.baseline",
            "config.py",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert scan.returncode == 0

    baseline_path = repo / ".secrets.baseline"
    baseline = json.loads(baseline_path.read_text())
    assert baseline["results"], "scan should have recorded the finding"
    for items in baseline["results"].values():
        for item in items:
            item["is_secret"] = False  # reviewed: synthetic test value, not a real credential
    baseline_path.write_text(json.dumps(baseline, indent=2) + "\n")
    # A baseline change must be staged/committed, like any other reviewed fix.
    _git(repo, "add", ".secrets.baseline")

    result = _run_hook(repo, "config.py")

    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------- #
# 4. A baseline/configuration error fails closed.
# --------------------------------------------------------------------- #


def test_missing_baseline_fails_closed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / ".secrets.baseline").unlink()

    result = _run_hook(repo)

    assert result.returncode != 0


def test_corrupt_baseline_fails_closed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / ".secrets.baseline").write_text("{not valid json")

    result = _run_hook(repo)

    assert result.returncode != 0


def test_corrupt_baseline_fails_check_security_sh(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / ".secrets.baseline").write_text("{not valid json")

    result = _run_check_security_sh(repo)

    assert result.returncode != 0
