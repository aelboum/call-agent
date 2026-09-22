"""Application service for `CallWorkflowExecution` (Phase 2.10).

Every function here follows the same discipline every other
`voiceagent.*.service` module already does: takes a verified
`voiceagent.tenancy.TenantContext`, opens its own `tenant_scope()`, and
never accepts a bare `tenant_id`. `voiceagent.workflows.executor.run_workflow()`
is the only caller in production, always crossing
`voiceagent.runtime.db.DatabaseBoundary.run()` -- exactly the same
"claim/update transactionally, commit, then do the external I/O, then
persist the result transactionally" shape
`voiceagent.followups.service.claim_due_follow_up()`/
`execute_due_follow_up()` already establish (brief TRANSACTIONS/CONCURRENCY:
"do not hold DB locks across external I/O").
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from core.audit_log import ActorType, AuditOutcome
from core.audit_log import record as record_audit_event
from pydantic import ValidationError

from voiceagent.agents.models import AgentVersion
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.calls.models import CallSession
from voiceagent.conversations.models import ConversationTurn
from voiceagent.db import IntegrityError, select
from voiceagent.followups import service as followup_service
from voiceagent.followups.models import CallOutcome, FollowUpAction
from voiceagent.tenancy import TenantContext, tenant_scope
from voiceagent.workflows.config import WorkflowConditionBranch, WorkflowDefinition
from voiceagent.workflows.errors import (
    WorkflowDefinitionInvalidError,
    WorkflowExecutionConflictError,
    WorkflowExecutionInProgressError,
    WorkflowExecutionNotFoundError,
    WorkflowNotConfiguredError,
)
from voiceagent.workflows.models import (
    WORKFLOW_EXECUTION_FAILURE_REASONS,
    CallWorkflowExecution,
)
from voiceagent.workflows.predicates import CallState, evaluate_predicate

__all__ = [
    "apply_follow_up_step",
    "apply_outcome_step",
    "begin_execution",
    "cancel_execution",
    "complete_execution",
    "evaluate_condition",
    "fail_execution",
    "get_execution",
    "load_workflow_definition",
    "record_step_advance",
]

_TOOL_CALL_TRANSFER_ID = "call.transfer"
_TOOL_CALL_HOLD_ID = "call.hold"


def load_workflow_definition(agent_version: AgentVersion) -> WorkflowDefinition:
    """Re-parse `agent_version.config["workflow"]` -- pure, DB-free. Raises
    `WorkflowNotConfiguredError` if the agent has none, or
    `WorkflowDefinitionInvalidError` if it no longer parses (see that
    error's own docstring for why this is defensive, not expected)."""
    workflow_config = agent_version.config.get("workflow")
    if workflow_config is None:
        raise WorkflowNotConfiguredError(agent_version.id)
    try:
        return WorkflowDefinition.model_validate(workflow_config)
    except ValidationError as exc:
        raise WorkflowDefinitionInvalidError(agent_version.id) from exc


def _require_call_row(session, tenant_id: uuid.UUID, call_session_id: uuid.UUID) -> CallSession:
    row = session.get(CallSession, call_session_id)
    if row is None or row.tenant_id != tenant_id:
        raise CallSessionNotFoundError(call_session_id)
    return row


def _find_execution_row(
    session, tenant_id: uuid.UUID, call_session_id: uuid.UUID
) -> CallWorkflowExecution | None:
    return (
        session.execute(
            select(CallWorkflowExecution)
            .where(CallWorkflowExecution.tenant_id == tenant_id)
            .where(CallWorkflowExecution.call_session_id == call_session_id)
        )
        .scalars()
        .first()
    )


def _require_running_row(
    session, tenant_id: uuid.UUID, execution_id: uuid.UUID
) -> CallWorkflowExecution:
    row = session.get(CallWorkflowExecution, execution_id)
    if row is None or row.tenant_id != tenant_id or row.status != "running":
        raise WorkflowExecutionConflictError(execution_id)
    return row


def begin_execution(
    context: TenantContext,
    call_session_id: uuid.UUID,
    agent_version: AgentVersion,
    definition: WorkflowDefinition,
) -> CallWorkflowExecution:
    """Claim this call's one-and-only `CallWorkflowExecution` row
    (`uq_call_workflow_executions_call_session`, the idempotency guard --
    module docstring). A redelivered claim for a call whose execution
    already reached a terminal status returns that row unchanged, never
    re-running the workflow; a redelivered claim while one is still
    `status='running'` raises `WorkflowExecutionInProgressError` rather than
    running a second, concurrent execution."""
    now = datetime.now(UTC)
    with tenant_scope(context) as session:
        _require_call_row(session, context.tenant_id, call_session_id)
        existing = _find_execution_row(session, context.tenant_id, call_session_id)
        if existing is not None:
            if existing.status == "running":
                raise WorkflowExecutionInProgressError(call_session_id)
            session.expunge(existing)
            return existing

        row = CallWorkflowExecution(
            tenant_id=context.tenant_id,
            call_session_id=call_session_id,
            agent_version_id=agent_version.id,
            workflow_config_hash=agent_version.config_hash,
            status="running",
            current_step_id=definition.entry_step_id,
            steps_executed=0,
            started_at=now,
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError:
            # A concurrent workflow.advance call won the race between our
            # own _find_execution_row() read and this INSERT -- the same
            # "lost the race" shape voiceagent.followups.service documents
            # for claim_due_follow_up()'s SELECT ... FOR UPDATE SKIP LOCKED,
            # here enforced by the UNIQUE constraint itself instead.
            raise WorkflowExecutionInProgressError(call_session_id) from None
        session.refresh(row)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.SYSTEM,
            action="workflow.execution_started",
            resource_type="call_workflow_execution",
            resource_id=str(row.id),
            outcome=AuditOutcome.SUCCESS,
            correlation_id=str(call_session_id),
            metadata={"agent_version_id": str(agent_version.id)},
        )

        session.expunge(row)
        return row


def record_step_advance(
    context: TenantContext,
    execution_id: uuid.UUID,
    *,
    current_step_id: str,
    steps_executed: int,
) -> CallWorkflowExecution:
    """Ownership-checked, transactional progress update -- `execution_id`
    must still be `status='running'`, or `WorkflowExecutionConflictError` is
    raised (mirrors `voiceagent.followups.service
    .complete_follow_up_execution()`'s own ownership guard)."""
    with tenant_scope(context) as session:
        row = _require_running_row(session, context.tenant_id, execution_id)
        row.current_step_id = current_step_id
        row.steps_executed = steps_executed
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def complete_execution(
    context: TenantContext, execution_id: uuid.UUID, *, final_step_id: str, steps_executed: int
) -> CallWorkflowExecution:
    now = datetime.now(UTC)
    with tenant_scope(context) as session:
        row = _require_running_row(session, context.tenant_id, execution_id)
        row.status = "completed"
        row.current_step_id = final_step_id
        row.steps_executed = steps_executed
        row.ended_at = now
        session.flush()
        session.refresh(row)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.SYSTEM,
            action="workflow.execution_completed",
            resource_type="call_workflow_execution",
            resource_id=str(row.id),
            outcome=AuditOutcome.SUCCESS,
            correlation_id=str(row.call_session_id),
            metadata={"steps_executed": steps_executed},
        )

        session.expunge(row)
        return row


def fail_execution(
    context: TenantContext,
    execution_id: uuid.UUID,
    *,
    reason: str,
    steps_executed: int,
) -> CallWorkflowExecution:
    if reason not in WORKFLOW_EXECUTION_FAILURE_REASONS:
        raise ValueError(f"invalid workflow execution failure reason: {reason!r}")
    now = datetime.now(UTC)
    with tenant_scope(context) as session:
        row = _require_running_row(session, context.tenant_id, execution_id)
        row.status = "failed"
        row.failure_reason = reason
        row.steps_executed = steps_executed
        row.ended_at = now
        session.flush()
        session.refresh(row)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.SYSTEM,
            action="workflow.execution_failed",
            resource_type="call_workflow_execution",
            resource_id=str(row.id),
            outcome=AuditOutcome.FAILURE,
            correlation_id=str(row.call_session_id),
            metadata={"steps_executed": steps_executed, "failure_reason": reason},
        )

        session.expunge(row)
        return row


def cancel_execution(
    context: TenantContext, execution_id: uuid.UUID
) -> CallWorkflowExecution | None:
    """Best-effort: called from `voiceagent.workflows.executor.run_workflow()`'s
    own `asyncio.CancelledError` handler (brief EXECUTION ENGINE: "support
    cancellation"). Returns `None` rather than raising when the row is
    already gone or already terminal -- the caller is unwinding from
    cancellation and must never have a secondary failure here mask the
    original one."""
    now = datetime.now(UTC)
    with tenant_scope(context) as session:
        row = session.get(CallWorkflowExecution, execution_id)
        if row is None or row.tenant_id != context.tenant_id or row.status != "running":
            return None
        row.status = "cancelled"
        row.ended_at = now
        session.flush()
        session.refresh(row)

        record_audit_event(
            tenant_id=context.tenant_id,
            actor_type=ActorType.SYSTEM,
            action="workflow.execution_cancelled",
            resource_type="call_workflow_execution",
            resource_id=str(row.id),
            outcome=AuditOutcome.SUCCESS,
            correlation_id=str(row.call_session_id),
            metadata={"steps_executed": row.steps_executed},
        )

        session.expunge(row)
        return row


def get_execution(context: TenantContext, call_session_id: uuid.UUID) -> CallWorkflowExecution:
    with tenant_scope(context) as session:
        row = _find_execution_row(session, context.tenant_id, call_session_id)
        if row is None:
            raise WorkflowExecutionNotFoundError(call_session_id)
        session.expunge(row)
        return row


def _tool_name(turn: ConversationTurn) -> str | None:
    if turn.tool_payload is None:
        return None
    name = turn.tool_payload.get("name")
    return str(name) if name is not None else None


def evaluate_condition(
    context: TenantContext, call_session_id: uuid.UUID, branch: WorkflowConditionBranch
) -> bool:
    """Fetch the small, closed set of facts `voiceagent.workflows.predicates`
    can evaluate `branch.predicate` against -- the same authoritative
    sources `voiceagent.call_analysis.service.build_call_analysis()` already
    documents, read fresh (never from a stale `CallAnalysis` snapshot, which
    is only built after the call ends)."""
    with tenant_scope(context) as session:
        call = _require_call_row(session, context.tenant_id, call_session_id)

        outcome_exists = (
            session.execute(
                select(CallOutcome.id)
                .where(CallOutcome.tenant_id == context.tenant_id)
                .where(CallOutcome.call_session_id == call_session_id)
            )
            .scalars()
            .first()
            is not None
        )

        follow_ups = (
            session.execute(
                select(FollowUpAction.type)
                .where(FollowUpAction.tenant_id == context.tenant_id)
                .where(FollowUpAction.call_session_id == call_session_id)
            )
            .scalars()
            .all()
        )

        tool_call_turns = (
            session.execute(
                select(ConversationTurn)
                .where(ConversationTurn.tenant_id == context.tenant_id)
                .where(ConversationTurn.call_session_id == call_session_id)
                .where(ConversationTurn.role == "tool_call")
            )
            .scalars()
            .all()
        )
        had_transfer = any(_tool_name(turn) == _TOOL_CALL_TRANSFER_ID for turn in tool_call_turns)
        had_hold = any(_tool_name(turn) == _TOOL_CALL_HOLD_ID for turn in tool_call_turns)

        elapsed_seconds = (
            (datetime.now(UTC) - call.started_at).total_seconds()
            if call.started_at is not None
            else None
        )

        state = CallState(
            contact_associated=call.contact_id is not None,
            outcome_exists=outcome_exists,
            follow_up_exists=len(follow_ups) > 0,
            appointment_exists=any(t == "appointment" for t in follow_ups),
            had_transfer=had_transfer,
            had_hold=had_hold,
            elapsed_seconds=elapsed_seconds,
        )

    return evaluate_predicate(branch.predicate, state, threshold_seconds=branch.threshold_seconds)


def apply_outcome_step(
    context: TenantContext, call_session_id: uuid.UUID, *, outcome: str, notes: str | None
) -> None:
    """Reuses `voiceagent.followups.service.set_call_outcome()` verbatim --
    idempotent, so a re-delivered step (a resumed execution after a conflict
    is impossible today, see module docstring, but this stays true
    regardless) never double-writes a different outcome."""
    followup_service.set_call_outcome(context, call_session_id, outcome=outcome, notes=notes)


def apply_follow_up_step(
    context: TenantContext,
    call_session_id: uuid.UUID,
    *,
    follow_up_type: str,
    description: str | None,
) -> None:
    """Reuses `voiceagent.followups.service.create_follow_up()` verbatim.
    Unlike `apply_outcome_step()`, this is **not** idempotent -- a workflow
    step reached twice creates two follow-ups. This cannot happen through
    the normal path (`begin_execution()`'s own claim guarantees a step runs
    at most once per execution); see `docs/PHASE-2.10-STATUS.md` "Known
    limitations" for the one scenario (a mid-execution process crash) where
    it remains a real, accepted gap."""
    followup_service.create_follow_up(
        context, call_session_id, type=follow_up_type, description=description
    )
