"""Domain errors for the controlled call-workflow execution lifecycle (Phase
2.10). Structural/tool-existence definition errors are `pydantic
.ValidationError`, raised by `voiceagent.workflows.config.WorkflowDefinition`
itself (see that module's docstring) -- these are the separate, smaller set
raised by `voiceagent.workflows.service`/`voiceagent.workflows.executor` once
a definition is already known to be well-formed.
"""

from __future__ import annotations

__all__ = [
    "WorkflowDefinitionInvalidError",
    "WorkflowError",
    "WorkflowExecutionConflictError",
    "WorkflowExecutionInProgressError",
    "WorkflowExecutionNotFoundError",
    "WorkflowNotConfiguredError",
]


class WorkflowError(Exception):
    """Base class for every controlled-call-workflow domain error."""


class WorkflowNotConfiguredError(WorkflowError):
    """The governing `AgentVersion` has no `config["workflow"]` -- `workflow
    .advance` is a no-op-shaped failure in this case, not a crash: an agent
    with no workflow configured simply has nothing to advance."""

    def __init__(self, agent_version_id: object) -> None:
        super().__init__(f"AgentVersion {agent_version_id} has no workflow configured")


class WorkflowDefinitionInvalidError(WorkflowError):
    """The governing `AgentVersion.config["workflow"]` no longer re-parses
    as a `voiceagent.workflows.config.WorkflowDefinition` -- should never
    happen (publish-time validation already required this to parse), kept
    as an explicit, defensively-handled failure rather than an unguarded
    crash mid-call, exactly the same discipline
    `voiceagent.followups.service._execute_appointment_follow_up()` applies
    to its own "guaranteed by construction, narrowed explicitly anyway"
    invariants."""

    def __init__(self, agent_version_id: object) -> None:
        super().__init__(f"AgentVersion {agent_version_id} has an invalid workflow definition")


class WorkflowExecutionInProgressError(WorkflowError):
    """A `CallWorkflowExecution` row already exists for this call and is
    still `status='running'` -- the idempotency guard
    (`voiceagent.workflows.service.begin_execution()`'s own `UNIQUE
    (call_session_id)`) refuses a second, concurrent execution rather than
    running the workflow twice."""

    def __init__(self, call_session_id: object) -> None:
        super().__init__(f"a workflow execution is already running for call {call_session_id}")


class WorkflowExecutionConflictError(WorkflowError):
    """The execution row no longer matches the caller's own execution id (or
    is no longer `status='running'`) when a step tried to advance it -- the
    identical "lost the race, do not overwrite the winner" shape as
    `voiceagent.followups.errors.FollowUpExecutionConflictError`."""

    def __init__(self, execution_id: object) -> None:
        super().__init__(f"CallWorkflowExecution {execution_id} is no longer owned by this run")


class WorkflowExecutionNotFoundError(WorkflowError):
    def __init__(self, call_session_id: object) -> None:
        super().__init__(f"no CallWorkflowExecution for call session: {call_session_id}")
