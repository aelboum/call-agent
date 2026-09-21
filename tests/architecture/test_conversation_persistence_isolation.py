"""Conversation-persistence isolation (Phase 2.5), mirroring
`tests/architecture/test_tool_gateway_isolation.py`'s own structure for the
identical reason: most of what matters here is already covered,
package-wide, by `tests/architecture/test_import_boundaries.py` (the
SQLAlchemy/psycopg, FreeSWITCH-confinement and provider-SDK fences already
scan every module under `voiceagent`, `voiceagent.conversations` and
`voiceagent.runtime.conversation_persistence` included). This file adds the
two properties specific to Phase 2.5: an explicit,
`voiceagent.conversations`-scoped restatement of "no forbidden import" for
documentation/traceability, and the *reverse*-direction fence -- the
`ConversationEngine` never imports conversation persistence -- which nothing
before Phase 2.5 needed to check.
"""

from __future__ import annotations

import ast
from pathlib import Path

CONVERSATIONS_ROOT = Path(__file__).resolve().parents[2] / "voiceagent" / "conversations"
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


def _conversations_files() -> list[Path]:
    return sorted(CONVERSATIONS_ROOT.rglob("*.py"))


def _engine_files() -> list[Path]:
    return sorted(ENGINE_ROOT.rglob("*.py"))


def test_conversations_package_exists_and_has_files() -> None:
    files = _conversations_files()
    assert len(files) >= 4, files


def test_no_conversations_module_imports_sqlalchemy_or_psycopg() -> None:
    forbidden = ("sqlalchemy", "psycopg", "psycopg2")
    offenders = [
        f"{path.name} -> {name}"
        for path in _conversations_files()
        for name in _imported_names(path)
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden)
    ]
    assert offenders == []


def test_no_conversations_module_imports_freeswitch_internals() -> None:
    forbidden = ("voiceagent.telephony.freeswitch", "ESL", "greenswitch", "switchio")
    offenders = [
        f"{path.name} -> {name}"
        for path in _conversations_files()
        for name in _imported_names(path)
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden)
    ]
    assert offenders == []


def test_no_conversations_module_imports_a_commercial_ai_provider_sdk() -> None:
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
        for path in _conversations_files()
        for name in _imported_names(path)
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden)
    ]
    assert offenders == []


def test_the_conversation_engine_never_imports_conversation_persistence() -> None:
    """Phase 2.5 brief section 15: the engine emits product-owned events
    only; it never persists one itself. `voiceagent.runtime.call_task` is
    the only module permitted to import `voiceagent.conversations` (directly
    or via `voiceagent.runtime.conversation_persistence`) -- checked by
    `pyproject.toml`'s matching import-linter contract; this AST scan is the
    same complementary second layer
    `test_the_conversation_engine_never_imports_the_tool_gateway` already
    establishes for the Phase 2.4 Tool Gateway fence."""
    offenders = [
        f"{path.name} -> {name}"
        for path in _engine_files()
        for name in _imported_names(path)
        if name == "voiceagent.conversations" or name.startswith("voiceagent.conversations.")
    ]
    assert offenders == []


def test_the_conversation_engine_never_imports_the_database_directly() -> None:
    """No engine module reaches `voiceagent.db` -- persistence, like every
    other database touch, happens on the far side of
    `voiceagent.runtime.call_task`'s own seam, never inside a
    `ConversationEngine` implementation (brief section 15: "no SQL/database
    code inside ... adapters")."""
    offenders = [
        f"{path.name} -> {name}"
        for path in _engine_files()
        for name in _imported_names(path)
        if name == "voiceagent.db" or name.startswith("voiceagent.db.")
    ]
    assert offenders == []
