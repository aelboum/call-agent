"""`ToolGateway`'s Phase 2.14 metrics recording -- `record_tool_execution()`
is called at the same terminal points `_audit()` already is, with the same
outcome string. `voiceagent.metrics.record_tool_execution` is monkeypatched
at its import site in `voiceagent.tools.gateway`, exactly the technique
`tests/tools/test_gateway.py` already uses for `rbac_can`/`record_audit_event`.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

import voiceagent.tools.handlers  # noqa: F401 -- registers the built-in tools
from voiceagent.agents.models import AgentVersion
from voiceagent.providers.engines.contracts import ToolCallRequested
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.telephony.fakes import FakeTelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.tools.gateway import ToolGateway


class _FakeServiceAccount:
    def __init__(self, account_id: uuid.UUID, name: str, status: str = "active") -> None:
        self.id = account_id
        self.name = name
        self.status = status


@pytest.fixture(autouse=True)
def resolve_service_account(monkeypatch):
    account = _FakeServiceAccount(uuid.uuid4(), "voiceagent-runtime")
    monkeypatch.setattr(
        "voiceagent.tools.gateway.list_service_accounts", lambda tenant_id: [account]
    )


@pytest.fixture(autouse=True)
def audit(monkeypatch):
    monkeypatch.setattr("voiceagent.tools.gateway.record_audit_event", lambda **kwargs: None)


@pytest.fixture
def allow_authorization(monkeypatch):
    monkeypatch.setattr("voiceagent.tools.gateway.rbac_can", lambda **kwargs: True)


@pytest.fixture
def recorded_executions(monkeypatch) -> list[tuple[str, float]]:
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(
        "voiceagent.tools.gateway.record_tool_execution",
        lambda *, outcome, duration_seconds: calls.append((outcome, duration_seconds)),
    )
    return calls


@pytest.fixture
def db():
    boundary = DatabaseBoundary(max_workers=2)
    yield boundary
    boundary.close()


@pytest.fixture
def context() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), membership_id=uuid.uuid4())


def _agent_version(context: TenantContext, *, tools: list[str]) -> AgentVersion:
    return AgentVersion(
        id=uuid.uuid4(),
        tenant_id=context.tenant_id,
        agent_id=uuid.uuid4(),
        version_number=1,
        status="published",
        config={"tools": [{"key": key, "config": {}} for key in tools]},
        config_hash="0" * 64,
    )


def _run_execute(gateway: ToolGateway, /, **kwargs):
    return asyncio.run(gateway.execute(**kwargs))


def test_successful_execution_records_succeeded_outcome(
    db, context, allow_authorization, recorded_executions
) -> None:
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    agent_version = _agent_version(context, tools=["call.hangup"])
    gateway = ToolGateway()

    _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=agent_version,
        call_ref=call_ref,
        telephony=telephony,
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(call_id="c1", name="call.hangup", arguments={}),
    )

    assert len(recorded_executions) == 1
    outcome, duration_seconds = recorded_executions[0]
    assert outcome == "succeeded"
    assert duration_seconds >= 0.0


def test_unknown_tool_records_denied_outcome_with_zero_duration(
    db, context, allow_authorization, recorded_executions
) -> None:
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    agent_version = _agent_version(context, tools=[])
    gateway = ToolGateway()

    _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=agent_version,
        call_ref=call_ref,
        telephony=telephony,
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(call_id="c1", name="no.such.tool", arguments={}),
    )

    assert recorded_executions == [("denied", 0.0)]


def test_tool_not_allowed_records_denied_outcome(
    db, context, allow_authorization, recorded_executions
) -> None:
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    agent_version = _agent_version(context, tools=[])  # call.hangup not in allowlist
    gateway = ToolGateway()

    _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=agent_version,
        call_ref=call_ref,
        telephony=telephony,
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(call_id="c1", name="call.hangup", arguments={}),
    )

    assert recorded_executions == [("denied", 0.0)]


def test_duplicate_call_records_duplicate_outcome_without_reexecuting(
    db, context, allow_authorization, recorded_executions
) -> None:
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    agent_version = _agent_version(context, tools=["call.hangup"])
    gateway = ToolGateway()
    call_session_id = uuid.uuid4()
    request = ToolCallRequested(call_id="c1", name="call.hangup", arguments={})

    _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=call_session_id,
        agent_version=agent_version,
        call_ref=call_ref,
        telephony=telephony,
        system_service_account_name="voiceagent-runtime",
        request=request,
    )
    _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=call_session_id,
        agent_version=agent_version,
        call_ref=call_ref,
        telephony=telephony,
        system_service_account_name="voiceagent-runtime",
        request=request,
    )

    assert [outcome for outcome, _ in recorded_executions] == ["succeeded", "duplicate"]
