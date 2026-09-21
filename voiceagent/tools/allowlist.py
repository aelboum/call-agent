"""The AgentVersion tool allowlist (Phase 2.4 brief section 4).

No new field, no new table: `voiceagent.agents.config.AgentConfig.tools`
(`list[ToolBinding]`, approved in Phase 2.0/2.1, `ToolBinding.key` already
documented there as "a tool *identifier*, e.g. 'calendar.book'" --
`tests/agents/test_config.py::test_no_field_named_like_a_secret_exists_on_the_schema`)
already *is* the immutable, published-AgentVersion-scoped tool allowlist this
phase needs. `AgentVersion.config` is the validated, immutable JSON snapshot
(ADR-0004); an allowlist read from it inherits that immutability for free --
changing a published version's tools requires a new `AgentVersion`, exactly
the brief's own rule, with no code in this module enforcing it (the database
trigger and `voiceagent.agents.service.publish_version()`'s own rules already
do).

The allowlist is an allowlist, never a denylist (brief section 4): a tool ID
not present in `agent_version.config["tools"]` is not permitted, full stop --
`is_tool_allowed()` returns `False` for both "explicitly not listed" and "the
config has no tools at all", never distinguishes the two, and never
consults `TOOL_REGISTRY` itself (that is `voiceagent.tools.gateway
.ToolGateway`'s own, separate "is this even a real tool" gate -- the two
checks are independent by design, ADR-0003 point 6).
"""

from __future__ import annotations

from voiceagent.agents.models import AgentVersion

__all__ = ["allowed_tool_ids", "is_tool_allowed"]


def allowed_tool_ids(agent_version: AgentVersion) -> frozenset[str]:
    """Every tool key bound in `agent_version`'s configuration. A binding's
    own `config` (per-tool tenant settings, e.g. a configured max-hold
    duration) is deliberately not surfaced here -- this module answers only
    "is this tool ID allowed", not "with what settings"; a handler that needs
    per-binding config is Phase 2.5+ scope, not built now."""
    tools = agent_version.config.get("tools") or []
    return frozenset(str(binding["key"]) for binding in tools)


def is_tool_allowed(agent_version: AgentVersion, tool_id: str) -> bool:
    return tool_id in allowed_tool_ids(agent_version)
