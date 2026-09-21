"""The media/audio-loop database-access fence (Phase 2.2 brief section 16:
"the media/audio loop must NEVER perform synchronous database I/O"; ADR-0008
point 2).

Two static checks, both on the parsed syntax tree (so a docstring mentioning
a forbidden name is never mistaken for a violation):

1. Neither `voiceagent.runtime.call_task` nor `voiceagent.runtime.supervisor`
   imports `voiceagent.db` or `voiceagent.tenancy.tenant_scope` directly --
   the only sanctioned path to the database from either module is
   `CallTaskDependencies.db: voiceagent.runtime.db.DatabaseBoundary`.
2. Inside `_run_pumps()` (the actual audio-adjacent loop in `call_task.py`)
   and its nested functions, no synchronous database-touching name may even
   be *referenced* -- stronger than "not called", because referencing one at
   all would be the first step toward calling it directly on the loop.
"""

from __future__ import annotations

import ast
import inspect

from voiceagent.runtime import call_task as call_task_module
from voiceagent.runtime import supervisor as supervisor_module

_FORBIDDEN_DIRECT_DB_IMPORTS = ("voiceagent.db", "voiceagent.tenancy.tenant_scope")

_SYNC_DB_TOUCHING_NAMES = frozenset(
    {
        "get_call_session",
        "get_agent_version",
        "transition_call_session",
        "claim_runtime_ownership",
        "authorize_call_data_access",
        "authorize_data_access",
        "persist_conversation_turn",
        "tenant_scope",
        "session_scope",
        "tenant_session_scope",
    }
)


def _imported_names(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _direct_db_import_violations(imported: set[str]) -> list[str]:
    return [
        name
        for name in imported
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in _FORBIDDEN_DIRECT_DB_IMPORTS
        )
    ]


def test_call_task_module_does_not_import_the_database_seam_directly() -> None:
    assert _direct_db_import_violations(_imported_names(call_task_module)) == []


def test_supervisor_module_does_not_import_the_database_seam_directly() -> None:
    assert _direct_db_import_violations(_imported_names(supervisor_module)) == []


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def test_the_audio_pump_never_references_a_sync_db_touching_name() -> None:
    tree = ast.parse(inspect.getsource(call_task_module))
    pump_function = _find_function(tree, "_run_pumps")
    referenced: set[str] = set()
    for node in ast.walk(pump_function):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
    assert referenced & _SYNC_DB_TOUCHING_NAMES == set()
