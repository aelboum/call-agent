"""`voiceagent.workflows.config.WorkflowDefinition` validation (Phase 2.10
brief VALIDATION/TESTING).
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

import voiceagent.tools.handlers  # noqa: F401 -- registers the built-in tools
from voiceagent.workflows.config import (
    MAX_WORKFLOW_STEPS,
    PREDICATES,
    WORKFLOW_TOOL_ID,
    WorkflowConditionBranch,
    WorkflowDefinition,
)


def _end(step_id: str = "end") -> dict:
    return {"step_id": step_id, "type": "end"}


def _tool(step_id: str, next_id: str, tool_id: str = "call.hold", **kwargs) -> dict:
    return {"step_id": step_id, "type": "tool", "tool_id": tool_id, "next": next_id, **kwargs}


def _condition(
    step_id: str, if_true: str, if_false: str, predicate: str = "contact_associated"
) -> dict:
    return {
        "step_id": step_id,
        "type": "condition",
        "branch": {"predicate": predicate, "if_true": if_true, "if_false": if_false},
    }


def _outcome(step_id: str, next_id: str, outcome: str = "resolved") -> dict:
    return {"step_id": step_id, "type": "outcome", "outcome": outcome, "next": next_id}


def _follow_up(step_id: str, next_id: str, follow_up_type: str = "manual_follow_up") -> dict:
    return {
        "step_id": step_id,
        "type": "follow_up",
        "follow_up_type": follow_up_type,
        "next": next_id,
    }


def test_predicates_constant_matches_the_literal_type() -> None:
    literal_annotation = WorkflowConditionBranch.model_fields["predicate"].annotation
    assert set(get_args(literal_annotation)) == PREDICATES


def test_outcome_values_matches_the_followups_module() -> None:
    """`voiceagent.workflows.config._OUTCOME_VALUES` is a local, literal
    copy of `voiceagent.followups.models.OUTCOME_VALUES` (see that module's
    own docstring for why it cannot be an import) -- this test is what
    keeps the two from silently drifting apart."""
    from voiceagent.followups.models import OUTCOME_VALUES
    from voiceagent.workflows.config import _OUTCOME_VALUES

    assert _OUTCOME_VALUES == OUTCOME_VALUES


def test_minimal_legal_workflow_parses() -> None:
    definition = WorkflowDefinition.model_validate({"entry_step_id": "e", "steps": [_end("e")]})
    assert definition.entry_step_id == "e"
    assert definition.step("e") is not None
    assert definition.step("missing") is None


def test_legal_workflow_exercising_every_step_type() -> None:
    payload = {
        "entry_step_id": "check",
        "steps": [
            _condition("check", if_true="hold", if_false="outcome"),
            _tool("hold", next_id="outcome"),
            _outcome("outcome", next_id="follow_up"),
            _follow_up("follow_up", next_id="end"),
            _end(),
        ],
    }
    definition = WorkflowDefinition.model_validate(payload)
    assert len(definition.steps) == 5


def test_unknown_step_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {"entry_step_id": "e", "steps": [{"step_id": "e", "type": "not-a-real-type"}]}
        )


def test_duplicate_step_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate({"entry_step_id": "e", "steps": [_end("e"), _end("e")]})


def test_unknown_entry_step_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate({"entry_step_id": "nope", "steps": [_end("e")]})


def test_unknown_transition_target_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {"entry_step_id": "t", "steps": [_tool("t", next_id="nowhere"), _end()]}
        )


def test_missing_terminal_step_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {"entry_step_id": "a", "steps": [_tool("a", next_id="a")]}
        )


def test_unreachable_step_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {"entry_step_id": "e", "steps": [_end("e"), _end("orphan")]}
        )


def test_cycle_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                "entry_step_id": "a",
                "steps": [_tool("a", next_id="b"), _tool("b", next_id="a"), _end()],
            }
        )


def test_condition_self_loop_cycle_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {"entry_step_id": "c", "steps": [_condition("c", if_true="c", if_false="e"), _end()]}
        )


def test_step_limit_is_enforced() -> None:
    steps = [_tool(f"t{i}", next_id=f"t{i + 1}") for i in range(MAX_WORKFLOW_STEPS)]
    steps.append(_end(f"t{MAX_WORKFLOW_STEPS}"))
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate({"entry_step_id": "t0", "steps": steps})


def test_a_workflow_at_exactly_the_step_limit_is_legal() -> None:
    steps = [_tool(f"t{i}", next_id=f"t{i + 1}") for i in range(MAX_WORKFLOW_STEPS - 1)]
    steps.append(_end(f"t{MAX_WORKFLOW_STEPS - 1}"))
    definition = WorkflowDefinition.model_validate({"entry_step_id": "t0", "steps": steps})
    assert len(definition.steps) == MAX_WORKFLOW_STEPS


def test_unknown_tool_id_is_a_bounded_string_not_a_structural_error() -> None:
    """An unknown `tool_id` parses -- structural validation does not depend
    on `voiceagent.tools.registry.TOOL_REGISTRY` (see module-level import
    boundary explained in `voiceagent.workflows.config`'s own docstring).
    `voiceagent.workflows.validation.validate_tool_references()` (tested in
    `tests/workflows/test_validation.py`) is where this is actually
    rejected, at draft-`AgentVersion`-creation time."""
    definition = WorkflowDefinition.model_validate(
        {
            "entry_step_id": "t",
            "steps": [_tool("t", next_id="e", tool_id="not.a.real.tool"), _end("e")],
        }
    )
    assert definition.step("t").tool_id == "not.a.real.tool"  # type: ignore[union-attr]


def test_workflow_tool_id_self_reference_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                "entry_step_id": "t",
                "steps": [_tool("t", next_id="e", tool_id=WORKFLOW_TOOL_ID), _end()],
            }
        )


def test_unsupported_predicate_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                "entry_step_id": "c",
                "steps": [
                    _condition("c", if_true="e", if_false="e", predicate="always_true"),
                    _end(),
                ],
            }
        )


def test_duration_predicate_requires_threshold_seconds() -> None:
    with pytest.raises(ValidationError):
        WorkflowConditionBranch.model_validate(
            {
                "predicate": "call_duration_at_least_seconds",
                "if_true": "a",
                "if_false": "b",
            }
        )


def test_threshold_seconds_is_rejected_for_a_non_duration_predicate() -> None:
    with pytest.raises(ValidationError):
        WorkflowConditionBranch.model_validate(
            {
                "predicate": "contact_associated",
                "threshold_seconds": 30,
                "if_true": "a",
                "if_false": "b",
            }
        )


def test_duration_predicate_with_threshold_seconds_is_legal() -> None:
    branch = WorkflowConditionBranch.model_validate(
        {
            "predicate": "call_duration_at_least_seconds",
            "threshold_seconds": 30,
            "if_true": "a",
            "if_false": "b",
        }
    )
    assert branch.threshold_seconds == 30


def test_unknown_outcome_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                "entry_step_id": "o",
                "steps": [_outcome("o", next_id="e", outcome="not-a-real-outcome"), _end()],
            }
        )


def test_unsupported_follow_up_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                "entry_step_id": "f",
                "steps": [
                    _follow_up("f", next_id="e", follow_up_type="appointment"),
                    _end(),
                ],
            }
        )


def test_tool_step_arguments_are_bounded_in_count() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                "entry_step_id": "t",
                "steps": [
                    _tool("t", next_id="e", arguments={f"k{i}": i for i in range(11)}),
                    _end(),
                ],
            }
        )


def test_tool_step_arguments_reject_nested_structures() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                "entry_step_id": "t",
                "steps": [_tool("t", next_id="e", arguments={"nested": {"a": 1}}), _end()],
            }
        )


def test_malformed_step_configuration_missing_required_field() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {"entry_step_id": "t", "steps": [{"step_id": "t", "type": "tool"}, _end()]}
        )


def test_workflow_is_frozen() -> None:
    definition = WorkflowDefinition.model_validate({"entry_step_id": "e", "steps": [_end("e")]})
    with pytest.raises(ValidationError):
        definition.entry_step_id = "other"


def test_extra_top_level_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {"entry_step_id": "e", "steps": [_end("e")], "nesting": {"steps": []}}
        )
