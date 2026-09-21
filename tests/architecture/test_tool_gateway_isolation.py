"""Tool Gateway isolation (Phase 2.4 brief section 14's "Isolation" and
"Security" test lists).

Most of what this file asserts is already covered, package-wide, by
`tests/architecture/test_import_boundaries.py` (the SQLAlchemy/psycopg,
FreeSWITCH-confinement and provider-SDK fences already scan every module
under `voiceagent`, `voiceagent.tools` included, once that package exists).
This file adds the two properties specific to Phase 2.4: an explicit,
`voiceagent.tools`-scoped restatement of "no forbidden import" for
documentation/traceability, and the *reverse*-direction fence -- the
`ConversationEngine` never imports the Tool Gateway -- which nothing before
Phase 2.4 needed to check because `voiceagent.tools` did not exist.
"""

from __future__ import annotations

import ast
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parents[2] / "voiceagent" / "tools"
ENGINE_ROOT = Path(__file__).resolve().parents[2] / "voiceagent" / "providers" / "engines"


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _tools_files() -> list[Path]:
    return sorted(TOOLS_ROOT.rglob("*.py"))


def _engine_files() -> list[Path]:
    return sorted(ENGINE_ROOT.rglob("*.py"))


def test_tools_package_exists_and_has_files() -> None:
    files = _tools_files()
    assert len(files) >= 6, files


def test_no_tool_module_imports_sqlalchemy_or_psycopg() -> None:
    forbidden = ("sqlalchemy", "psycopg", "psycopg2")
    offenders = [
        f"{path.name} -> {name}"
        for path in _tools_files()
        for name in _imported_names(path)
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden)
    ]
    assert offenders == []


def test_no_tool_module_imports_freeswitch_internals() -> None:
    forbidden = ("voiceagent.telephony.freeswitch", "ESL", "greenswitch", "switchio")
    offenders = [
        f"{path.name} -> {name}"
        for path in _tools_files()
        for name in _imported_names(path)
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden)
    ]
    assert offenders == []


def test_no_tool_module_imports_a_commercial_ai_provider_sdk() -> None:
    forbidden = (
        "openai",
        "elevenlabs",
        "deepgram",
        "google.generativeai",
        "google.genai",
        "mistralai",
        "groq",
        "pipecat",
    )
    offenders = [
        f"{path.name} -> {name}"
        for path in _tools_files()
        for name in _imported_names(path)
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden)
    ]
    assert offenders == []


def test_no_tool_module_imports_voiceagent_db_directly() -> None:
    """`voiceagent.tools.gateway` reaches the database only through the
    `DatabaseBoundary` it is handed by `voiceagent.runtime.call_task` --
    never `voiceagent.db`/`voiceagent.tenancy.tenant_scope` itself, the same
    rule `tests/architecture/test_runtime_db_boundary.py` enforces for the
    runtime's own audio pump."""
    forbidden = ("voiceagent.db", "voiceagent.tenancy.tenant_scope")
    offenders = [
        f"{path.name} -> {name}"
        for path in _tools_files()
        for name in _imported_names(path)
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden)
    ]
    assert offenders == []


def test_the_conversation_engine_never_imports_the_tool_gateway() -> None:
    """ADR-0003 point 1 / Phase 2.4 brief section 12: the engine never
    executes a tool itself and never knows which application service
    implements one. `voiceagent.runtime.call_task` is the only module
    permitted to import `voiceagent.tools` -- checked by
    `pyproject.toml`'s matching import-linter contract; this AST scan is
    the same complementary second layer
    `test_vendor_adapter_modules_are_confined_to_their_own_registry`
    already establishes for the Phase 2.3 vendor fences."""
    offenders = [
        f"{path.name} -> {name}"
        for path in _engine_files()
        for name in _imported_names(path)
        if name == "voiceagent.tools" or name.startswith("voiceagent.tools.")
    ]
    assert offenders == []
