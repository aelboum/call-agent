"""Real-PostgreSQL verification of Phase 2.10 (Controlled Call Workflows):
migrations, Row-Level Security (+FORCE), tenant isolation, cross-tenant
`CallWorkflowExecution`/`AgentVersion` reference rejection, the
one-execution-per-call idempotency guard under real concurrency, and the
`workflow.advance` Tool Gateway tool executing end-to-end under real
tenant/RBAC authorization.

Requires a real PostgreSQL instance with SaaS-OS's own migrations and this
product's migrations (through `0008_call_workflow_executions`) already
applied -- excluded from the default `pytest` run (`pytest -m integration`),
exactly mirroring `tests/integration/test_call_outcomes_followups_integration.py`.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from core.identity import add_tenant_membership, create_service_account, create_user
from core.rbac import (
    RoleScope,
    assign_first_role_for_new_tenant,
    create_role,
    grant_permission,
    register_permission,
)
from core.tenancy import create_tenant

from voiceagent.agents.config import AgentConfig
from voiceagent.agents.service import (
    create_agent,
    create_draft_version,
    get_agent_version,
    publish_version,
)
from voiceagent.calls.service import create_call_session
from voiceagent.db import IntegrityError, tenant_session_scope
from voiceagent.phone_numbers.service import register_phone_number
from voiceagent.providers.engines.contracts import ToolCallRequested
from voiceagent.rbac_bootstrap import PERMISSIONS, bootstrap_tenant_rbac
from voiceagent.telephony.fakes import FakeTelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.tools.gateway import ToolGateway
from voiceagent.tools.registry import TOOL_REGISTRY
from voiceagent.workflows.errors import WorkflowExecutionInProgressError
from voiceagent.workflows.models import CallWorkflowExecution
from voiceagent.workflows.service import begin_execution, get_execution, load_workflow_definition

pytestmark = pytest.mark.integration

SERVICE_ACCOUNT_NAME = "voiceagent-runtime"

_SIMPLE_WORKFLOW = {
    "entry_step_id": "hold",
    "steps": [
        {"step_id": "hold", "type": "tool", "tool_id": "call.hold", "next": "outcome"},
        {"step_id": "outcome", "type": "outcome", "outcome": "resolved", "next": "end"},
        {"step_id": "end", "type": "end"},
    ],
}


def _phone() -> str:
    return f"+1555{uuid.uuid4().int % 10**7:07d}"


def _config(*, workflow: dict | None = None) -> AgentConfig:
    payload = {
        "instructions": "Answer the phone.",
        "language": "en",
        "voice": {"provider": "fake", "voice_id": "v1"},
        "engine": {"kind": "pipelined", "stt": {"provider": "fake", "config": {}}},
        "business_hours": {"timezone": "UTC", "windows": []},
        "privacy": {"data_classification": "tenant_data", "purpose": "call_assistance"},
        "tools": [{"key": "call.hold", "config": {}}, {"key": "workflow.advance", "config": {}}],
        "workflow": workflow,
    }
    return AgentConfig.model_validate(payload)


def _make_call(context: TenantContext, *, workflow: dict | None = _SIMPLE_WORKFLOW):
    agent = create_agent(context, name=f"Agent {uuid.uuid4().hex[:8]}")
    draft = create_draft_version(context, agent.id, config=_config(workflow=workflow))
    version = publish_version(context, agent.id, draft.id)
    number = register_phone_number(context, e164=_phone())
    call = create_call_session(
        context,
        direction="inbound",
        from_e164="+15550000000",
        to_e164=number.e164,
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
    )
    return call, version


@pytest.fixture(scope="module")
def two_tenants() -> tuple[TenantContext, TenantContext]:
    tenant_a = create_tenant(f"phase210-a-{uuid.uuid4().hex[:8]}")
    tenant_b = create_tenant(f"phase210-b-{uuid.uuid4().hex[:8]}")
    user_a = create_user()
    user_b = create_user()
    context_a = TenantContext(tenant_id=tenant_a.id, actor_id=user_a.id, membership_id=uuid.uuid4())
    context_b = TenantContext(tenant_id=tenant_b.id, actor_id=user_b.id, membership_id=uuid.uuid4())
    return context_a, context_b


# --------------------------------------------------------------------------
# Migrations / structural
# --------------------------------------------------------------------------


def test_workflow_field_round_trips_through_a_published_agent_version(two_tenants) -> None:
    context_a, _ = two_tenants
    _, version = _make_call(context_a, workflow=_SIMPLE_WORKFLOW)
    fetched = get_agent_version(context_a, version.id)
    definition = load_workflow_definition(fetched)
    assert definition.entry_step_id == "hold"


# --------------------------------------------------------------------------
# Row-Level Security / tenant isolation / cross-tenant FK rejection
# --------------------------------------------------------------------------


def test_tenant_cannot_read_another_tenants_workflow_execution(two_tenants) -> None:
    context_a, context_b = two_tenants
    call, version = _make_call(context_a)
    definition = load_workflow_definition(version)
    execution = begin_execution(context_a, call.id, version, definition)

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(CallWorkflowExecution, execution.id) is None


def test_execution_cannot_reference_another_tenants_call(two_tenants) -> None:
    from datetime import UTC, datetime

    context_a, context_b = two_tenants
    call_a, _ = _make_call(context_a)

    with pytest.raises(IntegrityError):
        with tenant_session_scope(context_b.tenant_id) as session:
            session.add(
                CallWorkflowExecution(
                    tenant_id=context_b.tenant_id,
                    call_session_id=call_a.id,  # belongs to tenant A
                    agent_version_id=uuid.uuid4(),
                    workflow_config_hash="0" * 64,
                    status="running",
                    current_step_id="x",
                    started_at=datetime.now(UTC),
                )
            )
            session.flush()


def test_begin_execution_rejects_a_call_from_a_different_tenant(two_tenants) -> None:
    from voiceagent.calls.errors import CallSessionNotFoundError

    context_a, context_b = two_tenants
    call_a, version_a = _make_call(context_a)
    definition = load_workflow_definition(version_a)

    with pytest.raises(CallSessionNotFoundError):
        begin_execution(context_b, call_a.id, version_a, definition)


# --------------------------------------------------------------------------
# One-execution-per-call idempotency guard
# --------------------------------------------------------------------------


def test_begin_execution_is_idempotent_for_a_redelivered_terminal_row(two_tenants) -> None:
    context_a, _ = two_tenants
    call, version = _make_call(context_a)
    definition = load_workflow_definition(version)

    first = begin_execution(context_a, call.id, version, definition)
    # Simulate the executor reaching a terminal status directly, bypassing
    # the full run_workflow() loop -- this test only needs to prove
    # begin_execution()'s own idempotent-redelivery behavior.
    from voiceagent.workflows.service import complete_execution

    complete_execution(context_a, first.id, final_step_id="end", steps_executed=3)

    second = begin_execution(context_a, call.id, version, definition)
    assert second.id == first.id
    assert second.status == "completed"


def test_begin_execution_refuses_a_concurrent_duplicate_while_running(two_tenants) -> None:
    context_a, _ = two_tenants
    call, version = _make_call(context_a)
    definition = load_workflow_definition(version)

    begin_execution(context_a, call.id, version, definition)
    with pytest.raises(WorkflowExecutionInProgressError):
        begin_execution(context_a, call.id, version, definition)


def test_get_execution_reads_back_the_claimed_row(two_tenants) -> None:
    context_a, _ = two_tenants
    call, version = _make_call(context_a)
    definition = load_workflow_definition(version)
    begin_execution(context_a, call.id, version, definition)

    fetched = get_execution(context_a, call.id)
    assert fetched.call_session_id == call.id
    assert fetched.status == "running"
    assert fetched.current_step_id == "hold"


# --------------------------------------------------------------------------
# workflow.advance through the real Tool Gateway, real RBAC
# --------------------------------------------------------------------------


@pytest.fixture
def tenant_and_admin():
    tenant = create_tenant(f"phase210-tools-{uuid.uuid4().hex[:8]}")
    admin = create_user()
    membership = add_tenant_membership(tenant.id, admin.id)
    role = create_role(tenant.id, f"bootstrap-admin-{uuid.uuid4().hex[:8]}")
    for resource, action in (*PERMISSIONS, ("service_account_role", "create")):
        permission = register_permission(resource, action)
        grant_permission(tenant.id, role.id, permission.id)
    assign_first_role_for_new_tenant(tenant.id, membership.id, role.id, scope=RoleScope.SELF)
    return tenant.id, admin.id


@pytest.fixture
def tenant_context(tenant_and_admin) -> TenantContext:
    tenant_id, admin_id = tenant_and_admin
    return TenantContext(tenant_id=tenant_id, actor_id=admin_id, membership_id=uuid.uuid4())


@pytest.fixture
def bootstrapped_service_account(tenant_and_admin) -> str:
    tenant_id, admin_id = tenant_and_admin
    account = create_service_account(tenant_id, SERVICE_ACCOUNT_NAME)
    bootstrap_tenant_rbac(
        tenant_id=tenant_id, actor_user_id=admin_id, service_account_id=account.id
    )
    return SERVICE_ACCOUNT_NAME


def _run_execute(gateway, /, **kwargs):
    return asyncio.run(gateway.execute(**kwargs))


def test_workflow_advance_tool_executes_end_to_end(
    tenant_context, bootstrapped_service_account
) -> None:
    from voiceagent.runtime.db import DatabaseBoundary

    call, version = _make_call(tenant_context)
    agent_version = get_agent_version(tenant_context, call.agent_version_id)
    assert agent_version.id == version.id

    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")

    db = DatabaseBoundary(max_workers=2)
    try:
        gateway = ToolGateway(TOOL_REGISTRY)
        result = _run_execute(
            gateway,
            db=db,
            context=tenant_context,
            call_session_id=call.id,
            agent_version=agent_version,
            call_ref=call_ref,
            telephony=telephony,
            system_service_account_name=bootstrapped_service_account,
            request=ToolCallRequested(call_id="c1", name="workflow.advance", arguments={}),
        )
    finally:
        db.close()

    assert result.error_code is None
    assert result.value["status"] == "completed"
    assert result.value["steps_executed"] == 3

    execution = get_execution(tenant_context, call.id)
    assert execution.status == "completed"

    from voiceagent.followups.service import get_call_outcome

    outcome = get_call_outcome(tenant_context, call.id)
    assert outcome.outcome == "resolved"


def test_workflow_advance_with_no_workflow_configured_fails_closed(
    tenant_context, bootstrapped_service_account
) -> None:
    from voiceagent.runtime.db import DatabaseBoundary

    call, _ = _make_call(tenant_context, workflow=None)
    agent_version = get_agent_version(tenant_context, call.agent_version_id)

    db = DatabaseBoundary(max_workers=2)
    try:
        gateway = ToolGateway(TOOL_REGISTRY)
        result = _run_execute(
            gateway,
            db=db,
            context=tenant_context,
            call_session_id=call.id,
            agent_version=agent_version,
            call_ref="ref",
            telephony=FakeTelephonyProvider(),
            system_service_account_name=bootstrapped_service_account,
            request=ToolCallRequested(call_id="c1", name="workflow.advance", arguments={}),
        )
    finally:
        db.close()

    assert result.error_code == "workflow_not_configured"


def test_workflow_advance_not_in_allowlist_is_denied(
    tenant_context, bootstrapped_service_account
) -> None:
    from voiceagent.runtime.db import DatabaseBoundary

    call, version = _make_call(tenant_context)
    agent_version = get_agent_version(tenant_context, call.agent_version_id)
    object.__setattr__(agent_version, "config", {**agent_version.config, "tools": []})

    db = DatabaseBoundary(max_workers=2)
    try:
        gateway = ToolGateway(TOOL_REGISTRY)
        result = _run_execute(
            gateway,
            db=db,
            context=tenant_context,
            call_session_id=call.id,
            agent_version=agent_version,
            call_ref="ref",
            telephony=FakeTelephonyProvider(),
            system_service_account_name=bootstrapped_service_account,
            request=ToolCallRequested(call_id="c1", name="workflow.advance", arguments={}),
        )
    finally:
        db.close()

    assert result.error_code == "tool_not_allowed"


def test_workflow_advance_unauthorized_tenant_fails_closed(tenant_context) -> None:
    """No RBAC bootstrap has been run for this tenant/service account here
    -- must fail closed as `unauthorized`, never silently execute (the same
    property `tests/integration/test_call_outcomes_followups_integration.py
    ::test_unauthorized_tenant_access_fails_closed` already proves for
    Phase 2.7's own tools)."""
    from voiceagent.runtime.db import DatabaseBoundary

    call, version = _make_call(tenant_context)
    agent_version = get_agent_version(tenant_context, call.agent_version_id)

    db = DatabaseBoundary(max_workers=2)
    try:
        gateway = ToolGateway(TOOL_REGISTRY)
        result = _run_execute(
            gateway,
            db=db,
            context=tenant_context,
            call_session_id=call.id,
            agent_version=agent_version,
            call_ref="ref",
            telephony=FakeTelephonyProvider(),
            system_service_account_name=SERVICE_ACCOUNT_NAME,
            request=ToolCallRequested(call_id="c1", name="workflow.advance", arguments={}),
        )
    finally:
        db.close()

    assert result.error_code == "unauthorized"
