"""`voiceagent.workflows.executor.run_workflow()` -- hermetic (Phase 2.10
brief TESTING).

`voiceagent.workflows.service`'s own DB-touching functions are replaced by
an in-memory fake (`_FakeWorkflowStore`) -- the same "swap the thing that
would open a real session" shape `tests/tools/test_gateway.py` already uses
for `core.rbac.can()`/`core.audit_log.record()`. The real, DB-backed claim/
advance/RLS behavior is proven against a real PostgreSQL instance in
`tests/integration/test_workflow_execution_integration.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

import pytest

import voiceagent.tools.handlers  # noqa: F401 -- registers the built-in tools
from voiceagent.agents.models import AgentVersion
from voiceagent.providers.engines.contracts import ToolCallRequested, ToolResult
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.tenancy import TenantContext
from voiceagent.workflows import service as workflow_service
from voiceagent.workflows.config import WorkflowDefinition, WorkflowEndStep, WorkflowToolStep
from voiceagent.workflows.errors import (
    WorkflowExecutionConflictError,
    WorkflowExecutionInProgressError,
    WorkflowNotConfiguredError,
)
from voiceagent.workflows.executor import MAX_EXECUTION_TRANSITIONS, run_workflow


def _end(step_id: str = "end") -> dict:
    return {"step_id": step_id, "type": "end"}


def _tool(step_id: str, next_id: str, tool_id: str = "call.hold") -> dict:
    return {"step_id": step_id, "type": "tool", "tool_id": tool_id, "next": next_id}


def _condition(step_id: str, if_true: str, if_false: str) -> dict:
    return {
        "step_id": step_id,
        "type": "condition",
        "branch": {"predicate": "contact_associated", "if_true": if_true, "if_false": if_false},
    }


def _outcome(step_id: str, next_id: str) -> dict:
    return {"step_id": step_id, "type": "outcome", "outcome": "resolved", "next": next_id}


def _follow_up(step_id: str, next_id: str) -> dict:
    return {
        "step_id": step_id,
        "type": "follow_up",
        "follow_up_type": "manual_follow_up",
        "next": next_id,
    }


@dataclass
class _FakeExecution:
    id: uuid.UUID
    call_session_id: uuid.UUID
    status: str = "running"
    current_step_id: str = ""
    steps_executed: int = 0
    failure_reason: str | None = None


class _FakeWorkflowStore:
    """An in-memory stand-in for `voiceagent.workflows.service`'s DB-backed
    functions -- one execution row, the same ownership/idempotency rules."""

    def __init__(self) -> None:
        self.execution: _FakeExecution | None = None
        self.calls: list[str] = []
        self.condition_result = True
        self.conflict_on_step: int | None = None
        self.cancelled = False

    def load_workflow_definition(self, agent_version: AgentVersion) -> WorkflowDefinition:
        workflow = agent_version.config.get("workflow")
        if workflow is None:
            raise WorkflowNotConfiguredError(agent_version.id)
        return WorkflowDefinition.model_validate(workflow)

    def begin_execution(self, context, call_session_id, agent_version, definition):
        self.calls.append("begin")
        if self.execution is not None:
            if self.execution.status == "running":
                raise WorkflowExecutionInProgressError(call_session_id)
            return self.execution
        self.execution = _FakeExecution(
            id=uuid.uuid4(),
            call_session_id=call_session_id,
            current_step_id=definition.entry_step_id,
        )
        return self.execution

    def record_step_advance(self, context, execution_id, *, current_step_id, steps_executed):
        self.calls.append("advance")
        assert self.execution is not None
        if (
            self.execution.id != execution_id
            or self.execution.status != "running"
            or (self.conflict_on_step is not None and steps_executed >= self.conflict_on_step)
        ):
            raise WorkflowExecutionConflictError(execution_id)
        self.execution.current_step_id = current_step_id
        self.execution.steps_executed = steps_executed
        return self.execution

    def complete_execution(self, context, execution_id, *, final_step_id, steps_executed):
        self.calls.append("complete")
        assert self.execution is not None
        self.execution.status = "completed"
        self.execution.current_step_id = final_step_id
        self.execution.steps_executed = steps_executed
        return self.execution

    def fail_execution(self, context, execution_id, *, reason, steps_executed):
        self.calls.append("fail")
        assert self.execution is not None
        self.execution.status = "failed"
        self.execution.failure_reason = reason
        self.execution.steps_executed = steps_executed
        return self.execution

    def cancel_execution(self, context, execution_id):
        self.calls.append("cancel")
        self.cancelled = True
        if self.execution is not None and self.execution.status == "running":
            self.execution.status = "cancelled"
        return self.execution

    def evaluate_condition(self, context, call_session_id, branch):
        self.calls.append("condition")
        return self.condition_result

    def apply_outcome_step(self, context, call_session_id, *, outcome, notes):
        self.calls.append("outcome")

    def apply_follow_up_step(self, context, call_session_id, *, follow_up_type, description):
        self.calls.append("follow_up")


@dataclass
class _FakeToolGateway:
    responses: dict[str, ToolResult] = field(default_factory=dict)
    requests: list[ToolCallRequested] = field(default_factory=list)
    raise_cancelled: bool = False

    async def execute(self, **kwargs) -> ToolResult:
        request: ToolCallRequested = kwargs["request"]
        self.requests.append(request)
        if self.raise_cancelled:
            raise asyncio.CancelledError
        return self.responses.get(
            request.name, ToolResult(call_id=request.call_id, value={"ok": True})
        )


@pytest.fixture
def store(monkeypatch) -> _FakeWorkflowStore:
    fake = _FakeWorkflowStore()
    for name in (
        "load_workflow_definition",
        "begin_execution",
        "record_step_advance",
        "complete_execution",
        "fail_execution",
        "cancel_execution",
        "evaluate_condition",
        "apply_outcome_step",
        "apply_follow_up_step",
    ):
        monkeypatch.setattr(workflow_service, name, getattr(fake, name))
    return fake


@pytest.fixture
def db():
    boundary = DatabaseBoundary(max_workers=2)
    yield boundary
    boundary.close()


@pytest.fixture
def context() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), membership_id=uuid.uuid4())


def _agent_version(workflow: dict | None) -> AgentVersion:
    return AgentVersion(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        version_number=1,
        status="published",
        config={"tools": [], "workflow": workflow},
        config_hash="0" * 64,
    )


def _run(**kwargs):
    return asyncio.run(run_workflow(**kwargs))


def test_walks_every_step_type_to_completion(db, context, store) -> None:
    workflow = {
        "entry_step_id": "check",
        "steps": [
            _condition("check", if_true="hold", if_false="outcome"),
            _tool("hold", next_id="outcome"),
            _outcome("outcome", next_id="follow_up"),
            _follow_up("follow_up", next_id="end"),
            _end(),
        ],
    }
    gateway = _FakeToolGateway()
    result = _run(
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(workflow),
        tool_gateway=gateway,
        call_ref="ref",
        telephony=object(),
        system_service_account_name="voiceagent-runtime",
    )
    assert result.status == "completed"
    assert result.steps_executed == 5
    assert store.calls == [
        "begin",
        "condition",
        "advance",
        "advance",
        "outcome",
        "advance",
        "follow_up",
        "advance",
        "complete",
    ]
    assert gateway.requests[0].name == "call.hold"


def test_condition_false_branch_is_taken(db, context, store) -> None:
    store.condition_result = False
    workflow = {
        "entry_step_id": "check",
        "steps": [_condition("check", if_true="wrong", if_false="end"), _end(), _end("wrong")],
    }
    result = _run(
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(workflow),
        tool_gateway=_FakeToolGateway(),
        call_ref="ref",
        telephony=object(),
        system_service_account_name="voiceagent-runtime",
    )
    assert result.status == "completed"
    assert result.final_step_id == "end"


def test_tool_step_error_marks_execution_failed(db, context, store) -> None:
    workflow = {
        "entry_step_id": "t",
        "steps": [_tool("t", next_id="end"), _end()],
    }
    gateway = _FakeToolGateway(
        responses={"call.hold": ToolResult(call_id="x", error_code="telephony_error")}
    )
    result = _run(
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(workflow),
        tool_gateway=gateway,
        call_ref="ref",
        telephony=object(),
        system_service_account_name="voiceagent-runtime",
    )
    assert result.status == "failed"
    assert result.failure_reason == "tool_step_failed"
    assert result.error_code is None  # a value, not a raised ToolExecutionError


def test_workflow_not_configured(db, context, store) -> None:
    result = _run(
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(None),
        tool_gateway=_FakeToolGateway(),
        call_ref="ref",
        telephony=object(),
        system_service_account_name="voiceagent-runtime",
    )
    assert result.status == "not_configured"
    assert result.error_code == "workflow_not_configured"
    assert store.calls == []  # begin_execution never called -- no row created


def test_already_running_execution_is_reported_not_rerun(db, context, store) -> None:
    workflow = {"entry_step_id": "e", "steps": [_end("e")]}
    store.execution = _FakeExecution(
        id=uuid.uuid4(), call_session_id=uuid.uuid4(), status="running"
    )
    result = _run(
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(workflow),
        tool_gateway=_FakeToolGateway(),
        call_ref="ref",
        telephony=object(),
        system_service_account_name="voiceagent-runtime",
    )
    assert result.status == "running"
    assert result.error_code == "workflow_already_running"


def test_redelivered_terminal_execution_is_idempotent(db, context, store) -> None:
    workflow = {"entry_step_id": "e", "steps": [_end("e")]}
    store.execution = _FakeExecution(
        id=uuid.uuid4(),
        call_session_id=uuid.uuid4(),
        status="completed",
        current_step_id="e",
        steps_executed=1,
    )
    result = _run(
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(workflow),
        tool_gateway=_FakeToolGateway(),
        call_ref="ref",
        telephony=object(),
        system_service_account_name="voiceagent-runtime",
    )
    assert result.status == "completed"
    assert result.steps_executed == 1
    assert "complete" not in store.calls  # never re-run


def test_execution_conflict_mid_run_stops_and_reports_failed(db, context, store) -> None:
    store.conflict_on_step = 1
    workflow = {"entry_step_id": "t", "steps": [_tool("t", next_id="end"), _end()]}
    result = _run(
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(workflow),
        tool_gateway=_FakeToolGateway(),
        call_ref="ref",
        telephony=object(),
        system_service_account_name="voiceagent-runtime",
    )
    assert result.status == "failed"
    assert result.failure_reason == "execution_conflict"


def test_cancellation_is_recorded_and_reraised(db, context, store) -> None:
    workflow = {"entry_step_id": "t", "steps": [_tool("t", next_id="end"), _end()]}
    gateway = _FakeToolGateway(raise_cancelled=True)
    with pytest.raises(asyncio.CancelledError):
        _run(
            db=db,
            context=context,
            call_session_id=uuid.uuid4(),
            agent_version=_agent_version(workflow),
            tool_gateway=gateway,
            call_ref="ref",
            telephony=object(),
            system_service_account_name="voiceagent-runtime",
        )
    assert store.cancelled is True
    assert store.execution.status == "cancelled"


def test_max_execution_transitions_is_enforced(db, context, store, monkeypatch) -> None:
    """A definition that could never legally exist (its own step count
    already exceeds `MAX_WORKFLOW_STEPS`) is handed straight to the fake
    `load_workflow_definition()`, bypassing `WorkflowDefinition`'s own
    structural validator on purpose -- this proves the executor's own
    ceiling is a real, independent enforcement, not merely inherited for
    free from a definition that happens to always be small."""
    step_count = MAX_EXECUTION_TRANSITIONS + 5
    steps = {}
    for i in range(step_count):
        step_id, next_id = f"t{i}", f"t{i + 1}"
        steps[step_id] = WorkflowToolStep.model_validate(_tool(step_id, next_id))
    steps[f"t{step_count}"] = WorkflowEndStep.model_validate(_end(f"t{step_count}"))

    class _OversizedDefinition:
        entry_step_id = "t0"

        def step(self, step_id: str):
            return steps.get(step_id)

    monkeypatch.setattr(
        workflow_service, "load_workflow_definition", lambda agent_version: _OversizedDefinition()
    )

    result = _run(
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version({}),
        tool_gateway=_FakeToolGateway(),
        call_ref="ref",
        telephony=object(),
        system_service_account_name="voiceagent-runtime",
    )
    assert result.status == "failed"
    assert result.failure_reason == "max_steps_exceeded"
    assert result.steps_executed == MAX_EXECUTION_TRANSITIONS + 1
