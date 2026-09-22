"""RBAC permission declarations for `CallOutcome`/`FollowUpAction` (Phase
2.7 brief §17): two resources, matching `voiceagent.calendars.permissions`'s
own two-resources-one-package shape (a role can hold
`voiceagent.call_outcomes:read` without also getting
`voiceagent.follow_up_actions:cancel`). See `voiceagent.agents.permissions`
for why `register()` is never called at import or app-build time.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["CALL_OUTCOMES_RESOURCE", "FOLLOW_UP_ACTIONS_RESOURCE", "register"]

CALL_OUTCOMES_RESOURCE = "voiceagent.call_outcomes"
FOLLOW_UP_ACTIONS_RESOURCE = "voiceagent.follow_up_actions"


def register() -> None:
    for action in ("read", "create", "update"):
        register_permission(CALL_OUTCOMES_RESOURCE, action)
    for action in ("read", "create", "complete", "cancel"):
        register_permission(FOLLOW_UP_ACTIONS_RESOURCE, action)
