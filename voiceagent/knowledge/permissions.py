"""RBAC permission declarations for `KnowledgeSource`/`KnowledgeItem` (Phase
2.11): two resources, matching `voiceagent.followups.permissions`'s own
two-resources-one-package shape. There is deliberately no
`voiceagent.knowledge_admin`/"knowledge.admin" bypass resource -- every
action here is one of the specific, minimal verbs a management operation
actually needs (brief RBAC: "do not create a generic knowledge.admin
bypass").

`knowledge.search`, the Tool Gateway tool (`voiceagent.tools.handlers`), is
authorized the identical way every other tool is: through
`voiceagent.tools.permissions` (computed from `TOOL_REGISTRY`, already
included in `voiceagent.rbac_bootstrap.PERMISSIONS`) plus the published
`AgentVersion`'s own tool allowlist -- exactly the `workflow.advance`
precedent (`voiceagent.workflows.permissions`'s own docstring). No separate
`(RESOURCE_ITEMS, "search")` permission exists; a runtime search is not a
management operation.

See `voiceagent.agents.permissions` for why `register()` is never called at
import or app-build time.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["RESOURCE_ITEMS", "RESOURCE_SOURCES", "register"]

RESOURCE_SOURCES = "voiceagent.knowledge_sources"
RESOURCE_ITEMS = "voiceagent.knowledge_items"


def register() -> None:
    for action in ("read", "create", "update", "archive"):
        register_permission(RESOURCE_SOURCES, action)
    for action in ("read", "create", "update", "activate", "deactivate"):
        register_permission(RESOURCE_ITEMS, action)
