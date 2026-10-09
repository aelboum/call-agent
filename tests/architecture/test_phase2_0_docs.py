"""Documentation consistency for Phase 2.0 (research/decision phase only).

Phase 2.0 added architecture documentation and two ADRs, and changed nothing
else. The one fact a documentation-only phase can actually get wrong in a way
that matters later is a *stale or mistyped SaaS-OS commit SHA* quietly
entering a doc — exactly the kind of error this session already caught once
(a one-character-off SHA appeared in a user instruction and would have made
the pin unresolvable if copied verbatim). This test makes that class of error
fail CI instead of surviving into a future re-pin decision.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCS_ROOT = Path(__file__).resolve().parents[2] / "docs"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

CANONICAL_SHA = "ff550010e5eafecace7311038aadc99fcecfbe3d"

# Full-length SHAs of *this* repository's own HEAD, cited by a phase status
# report as "what HEAD was when this phase landed" (e.g. PHASE-2.31's "HEAD /
# origin/main: `cf11189...`"). These are not SaaS-OS pins and are legitimate
# even though they are 40 hex characters -- verified once via `git rev-parse
# <short-sha>` when added. CI checkouts are shallow, so this list can't be
# verified against `git` at test time; append to it only when a new phase
# report cites its own HEAD and the SHA has been checked against `git log`.
_OWN_REPO_HEAD_SHAS = {
    "cf11189a9fdafd80e58a1ab26b23b140f4e77904",  # PHASE-2.31's own HEAD
    "0f7c9d5d59576d8d7d6f57120e8f63de594ef805",  # PHASE-2.32's own HEAD
}

# A bare 40-hex-character token. Deliberately not anchored to "saas-os" or a
# URL, so it also catches a SHA pasted without its usual surrounding context.
_SHA_LIKE = re.compile(r"\b[0-9a-f]{40}\b")


def _markdown_files() -> list[Path]:
    return sorted(DOCS_ROOT.rglob("*.md"))


def test_docs_directory_is_not_empty() -> None:
    """Guards the guard: an empty scan must never pass silently."""
    assert len(_markdown_files()) >= 10


def test_every_sha_like_token_in_docs_is_the_canonical_pin() -> None:
    """No stale or mistyped SaaS-OS commit SHA anywhere under docs/. A wrong
    SHA in an ADR or the architecture report is worse than none: it reads as
    authoritative and would resolve to `git ls-remote`'s "not found" only if
    someone actually tried to install it."""
    offenders: list[str] = []
    for path in _markdown_files():
        for match in _SHA_LIKE.findall(path.read_text(encoding="utf-8")):
            if match != CANONICAL_SHA and match not in _OWN_REPO_HEAD_SHAS:
                offenders.append(f"{path.relative_to(DOCS_ROOT.parent)}: {match}")
    assert offenders == []


def test_pyproject_pins_the_canonical_sha() -> None:
    """The dependency declaration itself -- not just the docs describing
    it -- must name the same commit."""
    source = PYPROJECT.read_text(encoding="utf-8")
    assert f"@{CANONICAL_SHA}" in source


def test_phase_2_0_report_exists_and_names_its_own_scope() -> None:
    """A minimal existence check for this phase's actual deliverable."""
    report = DOCS_ROOT / "PHASE-2.0-ARCHITECTURE.md"
    assert report.is_file()
    content = report.read_text(encoding="utf-8")
    assert "OD-3" in content
    assert "OD-5" in content
    assert CANONICAL_SHA in content
