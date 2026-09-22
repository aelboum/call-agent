"""The controlled call-workflow executor (Phase 2.10).

`run_workflow()` is the one place the bounded step loop actually runs. It is
reached only from `voiceagent.tools.handlers._workflow_advance` -- itself
just one more Tool Gateway handler, dispatched off the audio pump by
`voiceagent.runtime.call_task._execute_and_submit_tool_call()`'s own
`asyncio.create_task()`, exactly like every other tool call
(`voiceagent.runtime.call_task`'s own module docstring: "no database access
happens inside the pump"). This module never imports
`voiceagent.providers.engines.*`, `voiceagent.telephony.freeswitch`, or
`voiceagent.runtime` -- it receives everything it needs (a
`DatabaseBoundary`, a `TenantContext`, the already-loaded `AgentVersion`, a
`ToolGateway` instance, a `CallRef`/`TelephonyProvider` pair) as plain
arguments, the same shape `voiceagent.tools.gateway.ToolGateway.execute()`
itself already takes.

**One bounded pass, not a resumable job.** A single `run_workflow()` call
walks the step graph from wherever `voiceagent.workflows.service
.begin_execution()` claims execution should start (always the workflow's own
`entry_step_id` -- there is no partial-resume, see that function's own
docstring) through to an `end` step, a failed step, cancellation, or
`MAX_EXECUTION_TRANSITIONS`, whichever comes first. Because
`voiceagent.workflows.config.WorkflowDefinition`'s own structural validator
already guarantees the step graph is acyclic and every step is reachable
from `entry_step_id`, `MAX_EXECUTION_TRANSITIONS` is never the *expected*
stop condition -- it exists purely as a defense-in-depth ceiling against a
future validator bug, not as this module's primary bound.

**Transaction boundaries** (brief TRANSACTIONS/CONCURRENCY): every
database-touching step here is one short `db.run(...)` call -- claim, then
(for a `tool` step) the external `tool_gateway.execute()` await happens with
no transaction open, then the result is persisted in a second, separate
`db.run(...)`. No lock is ever held across an `await`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from voiceagent.agents.models import AgentVersion
from voiceagent.providers.engines.contracts import ToolCallRequested
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.telephony.contracts import CallRef, TelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.workflows import service as workflow_service
from voiceagent.workflows.config import (
    MAX_WORKFLOW_STEPS,
    WorkflowConditionStep,
    WorkflowEndStep,
    WorkflowOutcomeStep,
    WorkflowToolStep,
)
from voiceagent.workflows.errors import (
    WorkflowDefinitionInvalidError,
    WorkflowExecutionConflictError,
    WorkflowExecutionInProgressError,
    WorkflowNotConfiguredError,
)

__all__ = ["MAX_EXECUTION_TRANSITIONS", "WorkflowRunResult", "run_workflow"]

_logger = logging.getLogger(__name__)

#: See module docstring's "One bounded pass" -- the acyclic, fully-reachable
#: step graph `WorkflowDefinition` already guarantees means this ceiling is
#: defense-in-depth, not the expected stop condition.
MAX_EXECUTION_TRANSITIONS = MAX_WORKFLOW_STEPS


@dataclass(frozen=True, slots=True)
class WorkflowRunResult:
    status: str
    steps_executed: int
    final_step_id: str | None
    failure_reason: str | None = None
    error_code: str | None = None


async def run_workflow(
    *,
    db: DatabaseBoundary,
    context: TenantContext,
    call_session_id: uuid.UUID,
    agent_version: AgentVersion,
    tool_gateway,  # voiceagent.tools.gateway.ToolGateway -- untyped to avoid an import cycle
    call_ref: CallRef,
    telephony: TelephonyProvider,
    system_service_account_name: str,
) -> WorkflowRunResult:
    try:
        definition = await db.run(workflow_service.load_workflow_definition, agent_version)
    except WorkflowNotConfiguredError:
        return WorkflowRunResult(
            status="not_configured",
            steps_executed=0,
            final_step_id=None,
            error_code="workflow_not_configured",
        )
    except WorkflowDefinitionInvalidError:
        return WorkflowRunResult(
            status="failed",
            steps_executed=0,
            final_step_id=None,
            failure_reason="invalid_workflow_definition",
            error_code="invalid_workflow_definition",
        )

    try:
        execution = await db.run(
            workflow_service.begin_execution, context, call_session_id, agent_version, definition
        )
    except WorkflowExecutionInProgressError:
        return WorkflowRunResult(
            status="running",
            steps_executed=0,
            final_step_id=None,
            error_code="workflow_already_running",
        )

    if execution.status != "running":
        # A redelivered workflow.advance for a call whose execution already
        # reached a terminal status -- idempotent: report it, never re-run.
        return WorkflowRunResult(
            status=execution.status,
            steps_executed=execution.steps_executed,
            final_step_id=execution.current_step_id,
            failure_reason=execution.failure_reason,
        )

    execution_id = execution.id
    current_step_id = execution.current_step_id
    steps_executed = execution.steps_executed

    try:
        for _ in range(MAX_EXECUTION_TRANSITIONS + 1):
            step = definition.step(current_step_id)
            if step is None:  # pragma: no cover -- guaranteed unreachable by
                # WorkflowDefinition's own structural validator; handled
                # explicitly rather than left to raise an unguarded KeyError.
                steps_executed += 1
                await db.run(
                    workflow_service.fail_execution,
                    context,
                    execution_id,
                    reason="invalid_workflow_definition",
                    steps_executed=steps_executed,
                )
                return WorkflowRunResult(
                    status="failed",
                    steps_executed=steps_executed,
                    final_step_id=current_step_id,
                    failure_reason="invalid_workflow_definition",
                )

            if isinstance(step, WorkflowEndStep):
                steps_executed += 1
                await db.run(
                    workflow_service.complete_execution,
                    context,
                    execution_id,
                    final_step_id=step.step_id,
                    steps_executed=steps_executed,
                )
                return WorkflowRunResult(
                    status="completed",
                    steps_executed=steps_executed,
                    final_step_id=step.step_id,
                )

            if isinstance(step, WorkflowConditionStep):
                is_true = await db.run(
                    workflow_service.evaluate_condition, context, call_session_id, step.branch
                )
                next_step_id = step.branch.if_true if is_true else step.branch.if_false
            elif isinstance(step, WorkflowToolStep):
                result = await tool_gateway.execute(
                    db=db,
                    context=context,
                    call_session_id=call_session_id,
                    agent_version=agent_version,
                    call_ref=call_ref,
                    telephony=telephony,
                    system_service_account_name=system_service_account_name,
                    request=ToolCallRequested(
                        call_id=f"workflow:{execution_id}:{step.step_id}",
                        name=step.tool_id,
                        arguments=step.arguments,
                    ),
                )
                if result.error_code is not None:
                    steps_executed += 1
                    await db.run(
                        workflow_service.fail_execution,
                        context,
                        execution_id,
                        reason="tool_step_failed",
                        steps_executed=steps_executed,
                    )
                    return WorkflowRunResult(
                        status="failed",
                        steps_executed=steps_executed,
                        final_step_id=step.step_id,
                        failure_reason="tool_step_failed",
                    )
                next_step_id = step.next
            elif isinstance(step, WorkflowOutcomeStep):
                await db.run(
                    workflow_service.apply_outcome_step,
                    context,
                    call_session_id,
                    outcome=step.outcome,
                    notes=step.notes,
                )
                next_step_id = step.next
            else:  # WorkflowFollowUpStep -- the one remaining step type
                await db.run(
                    workflow_service.apply_follow_up_step,
                    context,
                    call_session_id,
                    follow_up_type=step.follow_up_type,
                    description=step.description,
                )
                next_step_id = step.next

            steps_executed += 1
            try:
                await db.run(
                    workflow_service.record_step_advance,
                    context,
                    execution_id,
                    current_step_id=next_step_id,
                    steps_executed=steps_executed,
                )
            except WorkflowExecutionConflictError:
                return WorkflowRunResult(
                    status="failed",
                    steps_executed=steps_executed,
                    final_step_id=next_step_id,
                    failure_reason="execution_conflict",
                )
            current_step_id = next_step_id

        # MAX_EXECUTION_TRANSITIONS exhausted without reaching an 'end' step
        # -- see module docstring: unreachable given a validated
        # WorkflowDefinition, kept as an enforced ceiling regardless.
        await db.run(
            workflow_service.fail_execution,
            context,
            execution_id,
            reason="max_steps_exceeded",
            steps_executed=steps_executed,
        )
        return WorkflowRunResult(
            status="failed",
            steps_executed=steps_executed,
            final_step_id=current_step_id,
            failure_reason="max_steps_exceeded",
        )
    except asyncio.CancelledError:
        try:
            await db.run(workflow_service.cancel_execution, context, execution_id)
        except Exception:  # noqa: BLE001 -- unwinding from cancellation must
            # never let a secondary failure here mask the original one.
            _logger.exception(
                "failed to record cancellation for CallWorkflowExecution %s", execution_id
            )
        raise
