"""RBAC permission declaration for durable conversation history (Phase 2.5).

Read-only: no write API exists (`voiceagent.conversations.service
.persist_conversation_turn()`/`delete_conversation_turns()` are runtime-/
operator-invoked, not exposed through the tenant-facing API -- see
`voiceagent.agents.permissions` for why `register()` is never called at
import or app-build time).

A separate resource from `voiceagent.calls` (`voiceagent.call_sessions`)
deliberately: conversation *content* is more sensitive than call *metadata*
(Phase 2.5 brief section 13), so a role can be granted one without the other
rather than both riding on a single coarse permission.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["RESOURCE", "register"]

RESOURCE = "voiceagent.conversations"


def register() -> None:
    register_permission(RESOURCE, "read")
