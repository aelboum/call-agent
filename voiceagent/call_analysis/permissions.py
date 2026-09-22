"""RBAC permission declarations for `CallAnalysis` (Phase 2.8 brief §8): one
resource, two actions -- `read` (the default, primarily-read-oriented API)
and `rebuild` (a separate, explicit permission for the one write operation
this domain has, per the brief's own "give it a separate explicit
permission" instruction). See `voiceagent.agents.permissions` for why
`register()` is never called at import or app-build time.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["RESOURCE", "register"]

RESOURCE = "voiceagent.call_analysis"


def register() -> None:
    for action in ("read", "rebuild"):
        register_permission(RESOURCE, action)
