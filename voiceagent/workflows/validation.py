"""Tool-existence validation for a parsed `WorkflowDefinition` (Phase 2.10).

Deliberately separate from `voiceagent.workflows.config`'s own structural
validator -- see that module's docstring for why `voiceagent.workflows
.config` itself must stay free of any dependency on `voiceagent.tools`
(`voiceagent.agents.config`, which owns `AgentConfig.workflow`, is imported
by `voiceagent.providers.engines.factory`, which must never transitively
reach the Tool Gateway). `voiceagent.agents.service.create_draft_version()`
is the one caller of this module, in production -- a module the engine
factory does not import.
"""

from __future__ import annotations

from voiceagent.tools.registry import TOOL_REGISTRY
from voiceagent.workflows.config import WorkflowDefinition, WorkflowToolStep

__all__ = ["UnknownWorkflowToolError", "validate_tool_references"]


class UnknownWorkflowToolError(Exception):
    """A `tool` step names a `tool_id` that is not (or is no longer, though
    `voiceagent.tools.registry.TOOL_REGISTRY` is populated once, at process
    start, and never mutated afterward) a registered Tool Gateway tool."""

    def __init__(self, tool_id: str) -> None:
        super().__init__(f"unknown or unavailable workflow tool id: {tool_id!r}")
        self.tool_id = tool_id


def validate_tool_references(definition: WorkflowDefinition) -> None:
    """Raises `UnknownWorkflowToolError` for the first `tool` step, in
    definition order, whose `tool_id` is not in `TOOL_REGISTRY` -- called
    once, at draft-`AgentVersion`-creation time
    (`voiceagent.agents.service.create_draft_version()`), never at every
    execution: a published `AgentVersion` is immutable (ADR-0004), so a
    `tool_id` valid at publish time stays valid for the version's entire
    lifetime."""
    for step in definition.steps:
        if isinstance(step, WorkflowToolStep) and step.tool_id not in TOOL_REGISTRY:
            raise UnknownWorkflowToolError(step.tool_id)
