"""RBAC permission declarations for the Agent aggregate (SaaS-OS ADR-0004:
tool/route authorization is `core.rbac`'s, never a second system).

`register()` calls `core.rbac.register_permission()`, which writes to the
database (`session_scope()` inside it). It is therefore **never called at
import time or at application-build time** -- doing so would violate the
Phase 1 invariant that importing `voiceagent` and building the app open no
connection (`tests/architecture/test_import_side_effects.py`). It is a
one-time, idempotent deployment bootstrap step (`register_permission()` is
itself idempotent -- registering an already-registered pair returns the
existing row), invoked explicitly by an operator or a future migration/setup
script, not wired into `voiceagent.api.build_app()`.

Granting these permissions to a role is a separate, SaaS-OS-owned operation
(`core.rbac.grant_permission()`) this module does not perform -- out of
Phase 2.1's scope (see `docs/PHASE-2.1-STATUS.md`, "known limitations").
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["RESOURCE", "register"]

RESOURCE = "voiceagent.agents"


def register() -> None:
    for action in ("read", "write"):
        register_permission(RESOURCE, action)
