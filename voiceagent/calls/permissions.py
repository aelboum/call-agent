"""RBAC permission declarations for `CallSession`. Read-only through Phase
2.5 -- no write API existed yet (Phase 2.0 report §23.9). Phase 2.6 (brief
§16) adds exactly one write action, "associate" (the Call/Contact
association route), rather than reusing "read" for it. See
`voiceagent.agents.permissions` for why `register()` is never called at
import or app-build time.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["RESOURCE", "register"]

RESOURCE = "voiceagent.call_sessions"


def register() -> None:
    register_permission(RESOURCE, "read")
    register_permission(RESOURCE, "associate")
