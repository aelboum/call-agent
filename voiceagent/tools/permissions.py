"""RBAC permission declarations for the Tool Gateway (ADR-0003 point 6; Phase
2.4). One permission per tool -- `(RESOURCE, tool_id)` -- so a role can be
granted `call.hold` without also getting `call.transfer`, rather than one
coarse "execute any tool" permission that makes the AgentVersion allowlist
the *only* real gate (defeating ADR-0003 point 6's "two independent gates").

See `voiceagent.agents.permissions` for why `register()` is never called at
import or app-build time: every `voiceagent.*.permissions` module follows the
same rule, and `voiceagent.rbac_bootstrap` is the one place `register()` is
ever actually invoked.
"""

from __future__ import annotations

from core.rbac import register_permission

# Import for its registration side effect: TOOL_REGISTRY is empty until this
# module has run once. Importing it here (rather than requiring every caller
# of this module to remember to import it first) makes `register()` correct
# regardless of import order elsewhere in the process.
from voiceagent.tools import handlers as _handlers  # noqa: F401
from voiceagent.tools.registry import TOOL_REGISTRY

__all__ = ["RESOURCE", "register"]

#: The `core.rbac` resource every tool permission is declared under. Matches
#: `voiceagent.tools.gateway.RESOURCE` -- kept as one constant, re-exported,
#: rather than two string literals that could silently drift apart.
RESOURCE = "voiceagent.tools"


def register() -> None:
    """Idempotent (`register_permission()` is idempotent by SaaS-OS's own
    design). Declares one `(RESOURCE, tool_id)` permission per tool
    `voiceagent.tools.handlers` has registered into `TOOL_REGISTRY` --
    `voiceagent.tools.handlers` must already be imported (directly or
    transitively) before this runs, or the registry is empty and this is a
    silent no-op; `voiceagent.rbac_bootstrap.register_permissions()` imports
    it explicitly to guarantee that ordering.
    """
    for tool_id in TOOL_REGISTRY.known_tool_ids():
        register_permission(RESOURCE, tool_id)
