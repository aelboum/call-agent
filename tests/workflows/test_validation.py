"""`voiceagent.workflows.validation.validate_tool_references()` (Phase
2.10)."""

from __future__ import annotations

import pytest

import voiceagent.tools.handlers  # noqa: F401 -- registers the built-in tools
from voiceagent.workflows.config import WorkflowDefinition
from voiceagent.workflows.validation import UnknownWorkflowToolError, validate_tool_references


def _definition(tool_id: str) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "entry_step_id": "t",
            "steps": [
                {"step_id": "t", "type": "tool", "tool_id": tool_id, "next": "e"},
                {"step_id": "e", "type": "end"},
            ],
        }
    )


def test_a_known_registered_tool_id_passes() -> None:
    validate_tool_references(_definition("call.hold"))


def test_an_unknown_tool_id_is_rejected() -> None:
    with pytest.raises(UnknownWorkflowToolError) as excinfo:
        validate_tool_references(_definition("not.a.real.tool"))
    assert excinfo.value.tool_id == "not.a.real.tool"


def test_a_workflow_with_no_tool_steps_needs_no_registry_lookup() -> None:
    definition = WorkflowDefinition.model_validate(
        {"entry_step_id": "e", "steps": [{"step_id": "e", "type": "end"}]}
    )
    validate_tool_references(definition)  # does not raise
