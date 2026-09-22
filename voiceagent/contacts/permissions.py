"""RBAC permission declarations for `Contact` (Phase 2.6 brief §12). See
`voiceagent.agents.permissions` for why `register()` is never called at
import or app-build time.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["RESOURCE", "register"]

RESOURCE = "voiceagent.contacts"


def register() -> None:
    for action in ("read", "create"):
        register_permission(RESOURCE, action)
