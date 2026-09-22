"""The controlled call-workflow definition (Phase 2.10).

`WorkflowDefinition` is the exact, closed shape `AgentVersion.config["workflow"]`
validates against (`voiceagent.agents.config.AgentConfig.workflow`) -- it
inherits that field's immutability-once-published for free (ADR-0004): there
is no separate workflow version/lifecycle table.

**Five step types, and only five** (brief SCOPE): `tool`, `condition`,
`outcome`, `follow_up`, `end`. Every step has a stable `step_id` and an
explicit transition to another `step_id` -- there is no implicit fallthrough
and no step that omits where control goes next (except `end`, which has no
outgoing edge by construction).

**No expression language.** A `condition` step chooses between exactly two
named next steps based on one predicate from a fixed, closed vocabulary
(`PREDICATES`), evaluated against already-persisted call state
(`voiceagent.workflows.predicates`) -- never a user-supplied boolean
expression, script, or template.

**Structural validation happens once, at parse time**, via one
`model_validator` on `WorkflowDefinition` -- duplicate step ids, unknown
transitions, a missing terminal (`end`) step, unreachable steps, cycles, a
self-referencing `tool` step, an unsupported predicate, an unknown outcome
value, and an unsupported follow-up type. A `WorkflowDefinition` that fails
to parse fails the surrounding `AgentConfig` validation the same way any
other malformed field does (`pydantic.ValidationError`, Phase 2.1's own
established boundary).

**This module has no dependency on `voiceagent.tools`, `voiceagent.db`, or
`voiceagent.followups` -- deliberately.** `voiceagent.agents.config` (which
owns `AgentConfig.workflow`, this module's own `WorkflowDefinition`) is
imported by `voiceagent.providers.engines.factory` (for `EngineSelection`),
and that module must never transitively reach the Tool Gateway or the
database (import-linter's "The ConversationEngine never imports the Tool
Gateway"/"...conversation persistence" contracts). Two consequences:

* Whether a `tool` step's `tool_id` actually names a known, available Tool
  Gateway tool is **not** checked here -- that needs `voiceagent.tools
  .registry.TOOL_REGISTRY`, and is checked instead by `voiceagent.workflows
  .validation.validate_tool_references()`, called from `voiceagent.agents
  .service.create_draft_version()` (a module the engine factory does not
  import) before a draft version is ever persisted.
* `_OUTCOME_VALUES` below is a local, literal copy of `voiceagent.followups
  .models.OUTCOME_VALUES`'s values, not an import of that module (which
  would pull in `voiceagent.db`) -- kept in sync by
  `tests/workflows/test_workflow_config.py
  ::test_outcome_values_matches_the_followups_module`.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "MAX_WORKFLOW_STEPS",
    "PREDICATES",
    "WORKFLOW_FOLLOW_UP_TYPES",
    "WORKFLOW_TOOL_ID",
    "WorkflowConditionBranch",
    "WorkflowConditionStep",
    "WorkflowDefinition",
    "WorkflowEndStep",
    "WorkflowFollowUpStep",
    "WorkflowOutcomeStep",
    "WorkflowStep",
    "WorkflowToolStep",
]

#: The one Tool Gateway tool id this phase adds
#: (`voiceagent.tools.handlers`). A `tool` step may never name it: nesting a
#: workflow inside itself would make step-count/cycle bounds meaningless.
WORKFLOW_TOOL_ID = "workflow.advance"

#: Conservative, code-enforced bound on a workflow's total step count (brief
#: BOUNDS). Because the structural validator below also requires the step
#: graph to be acyclic and fully reachable from `entry_step_id`, this same
#: constant bounds the worst-case number of transitions one execution can
#: ever take (`voiceagent.workflows.executor.MAX_EXECUTION_TRANSITIONS`) --
#: a finite DAG has no path longer than its own node count.
MAX_WORKFLOW_STEPS = 20

#: The closed predicate vocabulary a `condition` step may evaluate
#: (`voiceagent.workflows.predicates`) -- never a user-supplied boolean
#: expression. Kept in sync with `WorkflowConditionBranch.predicate`'s own
#: `Literal` by `tests/workflows/test_config.py
#: ::test_predicates_constant_matches_the_literal_type`.
PREDICATES = frozenset(
    {
        "contact_associated",
        "contact_not_associated",
        "outcome_exists",
        "outcome_not_exists",
        "transfer_occurred",
        "hold_occurred",
        "follow_up_exists",
        "appointment_exists",
        "call_duration_at_least_seconds",
    }
)

#: `follow_up` steps only ever create the two follow-up types that need no
#: runtime-supplied calendar parameters (`voiceagent.followups.service
#: .create_follow_up()`'s own `type="appointment"` path needs a
#: `calendar_id`/`start_at`/`end_at` or an existing `calendar_event_id` --
#: none of which a static, published workflow step can sensibly supply). A
#: call that needs to schedule an appointment still can, through the
#: existing `call.create_follow_up`/`calendar.create_appointment` Tool
#: Gateway tools (a `tool` step, or the model calling them directly) -- this
#: is a deliberate narrowing of `voiceagent.followups.models.FOLLOW_UP_TYPES`,
#: not a second vocabulary.
WORKFLOW_FOLLOW_UP_TYPES = frozenset({"contact", "manual_follow_up"})

#: A local, literal copy of `voiceagent.followups.models.OUTCOME_VALUES`'s
#: values -- see module docstring for why this is not an import of that
#: module.
_OUTCOME_VALUES = frozenset(
    {
        "resolved",
        "appointment_scheduled",
        "follow_up_required",
        "no_answer",
        "wrong_number",
        "not_interested",
    }
)

_STEP_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,100}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkflowConditionBranch(_Strict):
    """One `condition` step's evaluation: a single predicate from
    `PREDICATES`, and the two step ids control moves to depending on its
    boolean result -- never more than two branches, never a nested
    condition."""

    predicate: Literal[
        "contact_associated",
        "contact_not_associated",
        "outcome_exists",
        "outcome_not_exists",
        "transfer_occurred",
        "hold_occurred",
        "follow_up_exists",
        "appointment_exists",
        "call_duration_at_least_seconds",
    ]
    #: Required, and only meaningful, for `call_duration_at_least_seconds`
    #: -- a bounded window (at most 24h), never an unbounded integer.
    threshold_seconds: int | None = Field(default=None, ge=0, le=86400)
    if_true: str = Field(pattern=_STEP_ID_PATTERN)
    if_false: str = Field(pattern=_STEP_ID_PATTERN)

    @model_validator(mode="after")
    def _validate_threshold(self) -> WorkflowConditionBranch:
        needs_threshold = self.predicate == "call_duration_at_least_seconds"
        if needs_threshold and self.threshold_seconds is None:
            raise ValueError(
                "predicate 'call_duration_at_least_seconds' requires threshold_seconds"
            )
        if not needs_threshold and self.threshold_seconds is not None:
            raise ValueError(
                f"threshold_seconds is only valid for predicate "
                f"'call_duration_at_least_seconds', not {self.predicate!r}"
            )
        return self


class WorkflowToolStep(_Strict):
    """Invoke exactly one existing, registered Tool Gateway tool
    (`voiceagent.tools.registry.TOOL_REGISTRY`) -- `arguments` is a bounded,
    flat map of JSON primitives fixed at publish time, never a model- or
    caller-supplied payload; the LLM never sees or controls it."""

    step_id: str = Field(pattern=_STEP_ID_PATTERN)
    type: Literal["tool"] = "tool"
    tool_id: str = Field(min_length=1, max_length=200)
    arguments: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    next: str = Field(pattern=_STEP_ID_PATTERN)

    @field_validator("arguments")
    @classmethod
    def _bound_arguments(
        cls, value: dict[str, str | int | float | bool | None]
    ) -> dict[str, str | int | float | bool | None]:
        if len(value) > 10:
            raise ValueError("a workflow tool step allows at most 10 argument entries")
        return value

    @field_validator("tool_id")
    @classmethod
    def _validate_tool_id(cls, value: str) -> str:
        # Whether `value` actually names a known, available Tool Gateway
        # tool is checked separately, by voiceagent.workflows.validation
        # .validate_tool_references() -- see module docstring.
        if value == WORKFLOW_TOOL_ID:
            raise ValueError(f"a workflow tool step may not reference {WORKFLOW_TOOL_ID!r} itself")
        return value


class WorkflowConditionStep(_Strict):
    step_id: str = Field(pattern=_STEP_ID_PATTERN)
    type: Literal["condition"] = "condition"
    branch: WorkflowConditionBranch


class WorkflowOutcomeStep(_Strict):
    """Reuses `voiceagent.followups.service.set_call_outcome()` -- never a
    second outcome vocabulary or write path."""

    step_id: str = Field(pattern=_STEP_ID_PATTERN)
    type: Literal["outcome"] = "outcome"
    outcome: str
    notes: str | None = Field(default=None, max_length=2000)
    next: str = Field(pattern=_STEP_ID_PATTERN)

    @field_validator("outcome")
    @classmethod
    def _validate_outcome(cls, value: str) -> str:
        if value not in _OUTCOME_VALUES:
            raise ValueError(f"unknown call outcome value: {value!r}")
        return value


class WorkflowFollowUpStep(_Strict):
    """Reuses `voiceagent.followups.service.create_follow_up()` -- never a
    second follow-up write path. See `WORKFLOW_FOLLOW_UP_TYPES` for why this
    is narrower than every `voiceagent.followups.models.FOLLOW_UP_TYPES`
    member."""

    step_id: str = Field(pattern=_STEP_ID_PATTERN)
    type: Literal["follow_up"] = "follow_up"
    follow_up_type: str
    description: str | None = Field(default=None, max_length=2000)
    next: str = Field(pattern=_STEP_ID_PATTERN)

    @field_validator("follow_up_type")
    @classmethod
    def _validate_follow_up_type(cls, value: str) -> str:
        if value not in WORKFLOW_FOLLOW_UP_TYPES:
            raise ValueError(f"unsupported workflow follow-up type: {value!r}")
        return value


class WorkflowEndStep(_Strict):
    """Terminal -- no outgoing transition. Every workflow must have at least
    one reachable `end` step (brief VALIDATION: "missing terminal path")."""

    step_id: str = Field(pattern=_STEP_ID_PATTERN)
    type: Literal["end"] = "end"


WorkflowStep = Annotated[
    WorkflowToolStep
    | WorkflowConditionStep
    | WorkflowOutcomeStep
    | WorkflowFollowUpStep
    | WorkflowEndStep,
    Field(discriminator="type"),
]


def _outgoing(step: WorkflowStep) -> tuple[str, ...]:
    if isinstance(step, WorkflowConditionStep):
        return (step.branch.if_true, step.branch.if_false)
    if isinstance(step, WorkflowEndStep):
        return ()
    # WorkflowToolStep | WorkflowOutcomeStep | WorkflowFollowUpStep
    return (step.next,)


def _reachable_from(entry: str, edges: dict[str, tuple[str, ...]]) -> set[str]:
    seen = {entry}
    stack = [entry]
    while stack:
        node = stack.pop()
        for target in edges.get(node, ()):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


def _has_cycle(entry: str, edges: dict[str, tuple[str, ...]]) -> bool:
    on_stack: set[str] = set()
    done: set[str] = set()

    def _visit(node: str) -> bool:
        if node in on_stack:
            return True
        if node in done:
            return False
        on_stack.add(node)
        for target in edges.get(node, ()):
            if _visit(target):
                return True
        on_stack.discard(node)
        done.add(node)
        return False

    return _visit(entry)


class WorkflowDefinition(_Strict):
    """The complete, closed workflow shape published as part of one
    `AgentVersion.config["workflow"]`."""

    entry_step_id: str = Field(pattern=_STEP_ID_PATTERN)
    steps: list[WorkflowStep] = Field(min_length=1, max_length=MAX_WORKFLOW_STEPS)

    @model_validator(mode="after")
    def _validate_structure(self) -> WorkflowDefinition:
        steps_by_id: dict[str, WorkflowStep] = {}
        for step in self.steps:
            if step.step_id in steps_by_id:
                raise ValueError(f"duplicate workflow step_id: {step.step_id!r}")
            steps_by_id[step.step_id] = step

        if self.entry_step_id not in steps_by_id:
            raise ValueError(f"unknown workflow entry_step_id: {self.entry_step_id!r}")

        has_end = any(isinstance(step, WorkflowEndStep) for step in self.steps)
        if not has_end:
            raise ValueError("workflow has no 'end' step (missing terminal path)")

        edges: dict[str, tuple[str, ...]] = {}
        for step in self.steps:
            targets = _outgoing(step)
            for target in targets:
                if target not in steps_by_id:
                    raise ValueError(
                        f"workflow step {step.step_id!r} transitions to unknown step {target!r}"
                    )
            edges[step.step_id] = targets

        reachable = _reachable_from(self.entry_step_id, edges)
        unreachable = sorted(set(steps_by_id) - reachable)
        if unreachable:
            raise ValueError(f"unreachable workflow step(s): {unreachable}")

        if _has_cycle(self.entry_step_id, edges):
            raise ValueError("workflow step graph contains a cycle")

        return self

    def step(self, step_id: str) -> WorkflowStep | None:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None
