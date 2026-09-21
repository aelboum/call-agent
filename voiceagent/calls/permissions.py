"""RBAC permission declarations for `CallSession`. Read-only in Phase 2.1 --
no write API exists yet (Phase 2.0 report §23.9). See
`voiceagent.agents.permissions` for why `register()` is never called at
import or app-build time.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["RESOURCE", "register"]

RESOURCE = "voiceagent.call_sessions"


def register() -> None:
    register_permission(RESOURCE, "read")
