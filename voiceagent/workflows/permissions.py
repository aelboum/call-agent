"""RBAC permission declarations for `CallWorkflowExecution` (Phase 2.10).

One resource, one action: `read`. There is deliberately no `execute`/
`advance`/`run` permission here -- `workflow.advance` is reached only
through the existing `voiceagent.tools` resource
(`voiceagent.tools.permissions`, computed from `TOOL_REGISTRY` and already
included in `voiceagent.rbac_bootstrap.PERMISSIONS`), gated by the
published `AgentVersion`'s own tool allowlist plus the Tool Gateway's own
RBAC check -- exactly the "must not require a new end-user 'execute
arbitrary workflow' permission" rule the brief states. See
`voiceagent.agents.permissions` for why `register()` is never called at
import or app-build time.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["RESOURCE", "register"]

RESOURCE = "voiceagent.call_workflows"


def register() -> None:
    register_permission(RESOURCE, "read")
