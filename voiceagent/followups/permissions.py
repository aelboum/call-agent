"""RBAC permission declarations for `CallOutcome`/`FollowUpAction` (Phase
2.7 brief §17; extended by Phase 2.9 brief §13): two resources, matching
`voiceagent.calendars.permissions`'s own two-resources-one-package shape (a
role can hold `voiceagent.call_outcomes:read` without also getting
`voiceagent.follow_up_actions:cancel`). See `voiceagent.agents.permissions`
for why `register()` is never called at import or app-build time.

**`retry`** (Phase 2.9) is the one new action: it authorizes
`POST /v1/follow-ups/{id}/retry` (`reprocess_follow_up()`) only -- brief
§13: "Add only the minimum permissions required... Do not add broad
execution permissions that allow arbitrary actions." There is deliberately
no `execute`/`claim` permission: `claim_due_follow_up()`/
`execute_due_follow_up()` are called by `voiceagent.followups.worker
.FollowUpWorker` only, never through a route a principal could invoke
directly (brief §10/§12), so authorizing them through `core.rbac` would
invent a capability with no caller.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["CALL_OUTCOMES_RESOURCE", "FOLLOW_UP_ACTIONS_RESOURCE", "register"]

CALL_OUTCOMES_RESOURCE = "voiceagent.call_outcomes"
FOLLOW_UP_ACTIONS_RESOURCE = "voiceagent.follow_up_actions"


def register() -> None:
    for action in ("read", "create", "update"):
        register_permission(CALL_OUTCOMES_RESOURCE, action)
    for action in ("read", "create", "complete", "cancel", "retry"):
        register_permission(FOLLOW_UP_ACTIONS_RESOURCE, action)
