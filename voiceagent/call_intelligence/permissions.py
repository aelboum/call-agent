"""RBAC permission declarations for `CallAiAnalysis` (Phase 2.12). One
resource, two actions -- `read` and `rebuild` -- the identical shape
`voiceagent.call_analysis.permissions` already establishes for Phase 2.8's
own deterministic analysis: reading stays separate from the one write
operation this domain has (brief RBAC: "Reading analysis must remain
distinct from modifying/rebuilding analysis"). There is no third,
"execute provider" permission -- a principal that holds `rebuild` requests a
new analysis *version*; only `voiceagent.call_intelligence.worker
.CallAiAnalysisWorker` ever actually invokes a provider, and it does so
under its own configured system actor, never under an end user's RBAC
grant (brief RBAC: "Do not allow arbitrary users to invoke provider
execution directly").

See `voiceagent.agents.permissions` for why `register()` is never called at
import or app-build time.
"""

from __future__ import annotations

from core.rbac import register_permission

__all__ = ["RESOURCE", "register"]

RESOURCE = "voiceagent.call_ai_analysis"


def register() -> None:
    for action in ("read", "rebuild"):
        register_permission(RESOURCE, action)
