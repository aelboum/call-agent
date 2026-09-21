"""The architecture fences, enforced by AST scan (Phase 0.1: ADR-0002, 0006, 0007).

This is the enforcement layer that works with nothing installed -- which
matters, because the most important fence (Pipecat) guards a package that is
deliberately *absent* in Phase 1, and import-linter cannot forbid a module it
cannot resolve. `pyproject.toml`'s import-linter contracts are the second
layer, covering the reachability cases an AST scan cannot see (a forbidden
import reached through an intermediate module).

Each test states the rule, the ADR, and the reason the rule exists -- because
a fence whose reason has been forgotten is a fence someone removes.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "voiceagent"
MIGRATIONS_ROOT = Path(__file__).resolve().parents[2] / "migrations"


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root.parent).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def _imported_names(path: Path) -> set[str]:
    """Every module name a file imports, including `from x.y import z` as
    both `x.y` and `x.y.z` so a fence cannot be slipped past by importing a
    submodule attribute."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _product_files() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _violations(
    forbidden_prefixes: tuple[str, ...], allowed_modules: tuple[str, ...] = ()
) -> list[str]:
    found: list[str] = []
    for path in _product_files():
        module = _module_name(path, PACKAGE_ROOT)
        if any(
            module == allowed or module.startswith(f"{allowed}.") for allowed in allowed_modules
        ):
            continue
        for imported in _imported_names(path):
            if any(
                imported == prefix or imported.startswith(f"{prefix}.")
                for prefix in forbidden_prefixes
            ):
                found.append(f"{module} -> {imported}")
    return found


def test_product_files_exist() -> None:
    """Guards the guards: an empty scan must never pass silently."""
    assert len(_product_files()) >= 15


def test_application_code_never_imports_sqlalchemy_or_psycopg() -> None:
    """ADR-0007. `infra.db` withholds `sqlalchemy.text` and `sqlalchemy.func`
    because either one lets application code run
    `set_config('app.tenant_id', ...)` inside a tenant-scoped session and
    bypass Row-Level Security for the rest of the transaction. Importing
    SQLAlchemy directly would restore that capability class in a product whose
    most sensitive data is call recordings and transcripts."""
    assert _violations(("sqlalchemy", "psycopg", "psycopg2")) == []


def test_persistence_goes_through_the_product_seam() -> None:
    """ADR-0007: `infra.db` is imported by `voiceagent.db` and nowhere else,
    so an upstream surface change on a future re-pin is a one-file fix."""
    assert _violations(("infra.db",), allowed_modules=("voiceagent.db",)) == []


def test_pipecat_is_confined_to_its_adapter() -> None:
    """ADR-0006: the `ConversationEngine` contract is product-owned and
    framework-free, and Pipecat may be imported only from
    `voiceagent.providers.engines.pipecat`. Enforced now, while Pipecat is
    not installed, because the fence has to exist before the first import
    does -- that is what keeps the runtime rewrite Phase 0 prohibits
    structurally impossible."""
    assert (
        _violations(("pipecat",), allowed_modules=("voiceagent.providers.engines.pipecat",)) == []
    )


def test_freeswitch_internals_are_confined_to_their_adapter() -> None:
    """ADR-0002 amendment: ESL, `mod_audio_stream` framing and every other
    FreeSWITCH concept live behind `voiceagent.telephony.freeswitch`. Domain
    code sees `TelephonyProvider`/`MediaProvider` and a normalized
    `HangupCause`, never a carrier string."""
    forbidden = ("ESL", "greenswitch", "switchio", "voiceagent.telephony.freeswitch")
    assert _violations(forbidden, allowed_modules=("voiceagent.telephony.freeswitch",)) == []


def test_object_storage_sdk_is_not_imported_anywhere_yet() -> None:
    """Phase 0 report section 2.4 G-2 / section 17 of the Phase 1 brief:
    `boto3` reaches the product only inside a future storage adapter, never in
    an arbitrary application module. Phase 1 has no adapter, so the correct
    count is zero."""
    assert _violations(("boto3", "botocore")) == []


def test_migrations_may_import_sqlalchemy() -> None:
    """The other half of ADR-0007: migration code owns physical PostgreSQL
    types. This asserts the split is real -- that SQLAlchemy is available
    where it belongs -- so the rule above cannot be satisfied by simply having
    no migrations."""
    env_imports = _imported_names(MIGRATIONS_ROOT / "env.py")
    assert "sqlalchemy" in env_imports


def test_migrations_do_not_autogenerate_against_platform_metadata() -> None:
    """ADR-0007 decision point 5: `Base.metadata` is shared with every SaaS-OS
    table, so autogenerate would compare the platform's schema against this
    product's model set and propose dropping tables this history does not
    own."""
    source = (MIGRATIONS_ROOT / "env.py").read_text(encoding="utf-8")
    assert "target_metadata = None" in source


def test_create_all_is_never_called() -> None:
    """ADR-0007: schemas come from migrations, never from model metadata --
    in any environment, including tests.

    Checked on the parsed syntax tree, not on the file text: a docstring
    explaining the rule must not be mistaken for a violation of it.
    """
    offenders: list[str] = []
    for path in _product_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name == "create_all":
                offenders.append(_module_name(path, PACKAGE_ROOT))
    assert offenders == []
