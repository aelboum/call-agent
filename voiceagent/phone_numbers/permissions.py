"""RBAC permission declarations for `PhoneNumber`. See
`voiceagent.agents.permissions` for why `register()` is never called at
import or app-build time.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["RESOURCE", "register"]

RESOURCE = "voiceagent.phone_numbers"


def register() -> None:
    for action in ("read", "write"):
        register_permission(RESOURCE, action)
