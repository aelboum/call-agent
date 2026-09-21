"""Real-PostgreSQL verification of `voiceagent.tools.gateway.ToolGateway`
(Phase 2.4 brief section 15): the paths `tests/tools/test_gateway.py`
deliberately monkeypatches (`core.rbac.can()`, `core.audit_log.record()`)
run for real here, plus AgentVersion tool-allowlist persistence, cross-tenant
service-account rejection, and one full `run_call_task()` execution that
proves the `ConversationEngine -> ToolCallRequested -> ToolGateway ->
ToolResult -> ConversationEngine` flow end to end against real
`TelephonyProvider`/audit-log-backed authorization.

Excluded from the default `pytest` run (`pytest -m integration`).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from core.audit_log import list as list_audit_log
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
from voiceagent.config.settings import AiProviderSettings
from voiceagent.conversations.service import list_conversation_turns
from voiceagent.phone_numbers.service import register_phone_number
from voiceagent.providers.engines.component_fakes import (
    FakeLlmProvider,
    FakeSttProvider,
    FakeTtsProvider,
)
from voiceagent.providers.engines.contracts import ToolCallRequested, TurnEnded
from voiceagent.providers.engines.pipelined import PipelinedEngine
from voiceagent.rbac_bootstrap import PERMISSIONS, bootstrap_tenant_rbac
from voiceagent.runtime.call_task import CallTaskDependencies, CancellationSignal, run_call_task
from voiceagent.runtime.conversation_persistence import ConversationPersistence
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.runtime.privacy import StaticAiDataPolicySource
from voiceagent.telephony.fakes import FakeMediaProvider, FakeTelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.tools.gateway import ToolGateway

#: Matches `RuntimeSettings.system_service_account_name`'s own default --
#: `voiceagent.tools.gateway.ToolGateway.execute()` resolves a service
#: account by this name, per tenant (see that settings field's own
#: docstring for why a fixed id cannot work across tenants).
SERVICE_ACCOUNT_NAME = "voiceagent-runtime"

pytestmark = pytest.mark.integration


def _config(*, tools: list[str]) -> AgentConfig:
    payload = {
        "instructions": "Answer the phone.",
        "language": "en",
        "voice": {"provider": "fake", "voice_id": "v1"},
        "engine": {"kind": "pipelined", "stt": {"provider": "fake", "config": {}}},
        "tools": [{"key": key, "config": {}} for key in tools],
        "business_hours": {"timezone": "UTC", "windows": []},
        "privacy": {"data_classification": "tenant_data", "purpose": "conversation"},
    }
    return AgentConfig.model_validate(payload)


def _permissive_policy_source() -> StaticAiDataPolicySource:
    return StaticAiDataPolicySource(
        AiProviderSettings(
            eligible_providers=("fake",),
            allowed_data_classifications=("tenant_data",),
            allowed_purposes=("conversation",),
        )
    )


@pytest.fixture
def tenant_and_admin():
    tenant = create_tenant(f"phase24-{uuid.uuid4().hex[:8]}")
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
def system_actor_user_id() -> uuid.UUID:
    return create_user().id


@pytest.fixture
def bootstrapped_service_account(tenant_and_admin) -> str:
    """A real `core.identity.ServiceAccount`, named `SERVICE_ACCOUNT_NAME`
    (the name `ToolGateway.execute()` resolves by, per tenant), granted
    every Phase 2.4 tool permission via `bootstrap_tenant_rbac()` -- not a
    test double. Returns the *name* `ToolGateway` is given, not the id --
    the id is resolved internally, from `(tenant_id, name)`."""
    tenant_id, admin_id = tenant_and_admin
    account = create_service_account(tenant_id, SERVICE_ACCOUNT_NAME)
    bootstrap_tenant_rbac(
        tenant_id=tenant_id, actor_user_id=admin_id, service_account_id=account.id
    )
    return SERVICE_ACCOUNT_NAME


def _make_call(context: TenantContext, *, tools: list[str]):
    agent = create_agent(context, name=f"Agent {uuid.uuid4().hex[:8]}")
    draft = create_draft_version(context, agent.id, config=_config(tools=tools))
    version = publish_version(context, agent.id, draft.id)
    number = register_phone_number(context, e164=f"+1555{uuid.uuid4().int % 10**7:07d}")
    return create_call_session(
        context,
        direction="inbound",
        from_e164="+15550000000",
        to_e164=number.e164,
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
    )


# --------------------------------------------------------------------------
# Real authorization + real audit (the paths tests/tools/test_gateway.py
# monkeypatches)
# --------------------------------------------------------------------------


def test_a_bootstrapped_service_account_can_execute_an_allowed_tool(
    tenant_context, bootstrapped_service_account
) -> None:
    call = _make_call(tenant_context, tools=["call.hangup"])
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    db = DatabaseBoundary(max_workers=2)
    gateway = ToolGateway()
    agent_version = get_agent_version(tenant_context, call.agent_version_id)

    async def scenario():
        return await gateway.execute(
            db=db,
            context=tenant_context,
            call_session_id=call.id,
            agent_version=agent_version,
            call_ref=call_ref,
            telephony=telephony,
            system_service_account_name=bootstrapped_service_account,
            request=ToolCallRequested(call_id="c1", name="call.hangup", arguments={}),
        )

    try:
        result = asyncio.run(scenario())
    finally:
        db.close()

    assert result.error_code is None
    assert result.value == {"hung_up": True}
    assert call_ref not in telephony.live_calls

    entries = list_audit_log(tenant_context.tenant_id, resource_type="tool_call")
    statuses = [(entry.entry_metadata or {}).get("status") for entry in entries]
    assert "started" in statuses
    assert "succeeded" in statuses


def test_an_unbootstrapped_service_account_is_denied_for_real(
    tenant_and_admin, tenant_context
) -> None:
    """A real, correctly-named `voiceagent-runtime` service account exists
    for this tenant (so resolution succeeds) but was never granted any
    Phase 2.4 tool permission -- `core.rbac.can()` itself denies it."""
    tenant_id, _admin_id = tenant_and_admin
    create_service_account(tenant_id, SERVICE_ACCOUNT_NAME)  # never bootstrapped
    call = _make_call(tenant_context, tools=["call.hangup"])
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    db = DatabaseBoundary(max_workers=2)
    gateway = ToolGateway()

    agent_version = get_agent_version(tenant_context, call.agent_version_id)

    async def scenario():
        return await gateway.execute(
            db=db,
            context=tenant_context,
            call_session_id=call.id,
            agent_version=agent_version,
            call_ref=call_ref,
            telephony=telephony,
            system_service_account_name=SERVICE_ACCOUNT_NAME,
            request=ToolCallRequested(call_id="c1", name="call.hangup", arguments={}),
        )

    try:
        result = asyncio.run(scenario())
    finally:
        db.close()

    assert result.error_code == "unauthorized"
    assert call_ref in telephony.live_calls  # nothing executed


def test_a_tenant_with_no_bootstrap_is_denied_even_though_another_tenant_is_fully_set_up(
    tenant_context, bootstrapped_service_account
) -> None:
    """`bootstrapped_service_account`/`tenant_context` are fully bootstrapped
    (a real, `voiceagent-runtime`-named, authorized service account exists).
    A second, *un*bootstrapped tenant sharing the exact same configured
    service-account *name* must not borrow that authorization -- resolution
    and authorization are both scoped to the call's own tenant
    (`_resolve_service_account()`'s own `tenant_id` parameter; `core.rbac
    .can()`'s `actor_tenant_id`). This is the per-tenant analogue of the
    cross-tenant-attribution bug Phase 2.4's own integration testing caught
    in an earlier, single-global-id design -- see
    `docs/PHASE-2.4-STATUS.md`."""
    other_tenant = create_tenant(f"phase24-other-{uuid.uuid4().hex[:8]}")
    other_context = TenantContext(
        tenant_id=other_tenant.id, actor_id=create_user().id, membership_id=uuid.uuid4()
    )
    call = _make_call(other_context, tools=["call.hangup"])  # never bootstrapped
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    db = DatabaseBoundary(max_workers=2)
    gateway = ToolGateway()

    agent_version = get_agent_version(other_context, call.agent_version_id)

    async def scenario():
        return await gateway.execute(
            db=db,
            context=other_context,
            call_session_id=call.id,
            agent_version=agent_version,
            call_ref=call_ref,
            telephony=telephony,
            system_service_account_name=bootstrapped_service_account,  # same name, other tenant
            request=ToolCallRequested(call_id="c1", name="call.hangup", arguments={}),
        )

    try:
        result = asyncio.run(scenario())
    finally:
        db.close()

    assert result.error_code == "unauthorized"
    assert call_ref in telephony.live_calls  # nothing executed


def test_no_service_account_provisioned_for_this_tenant_fails_closed_for_real(
    tenant_context,
) -> None:
    call = _make_call(tenant_context, tools=["call.hangup"])
    db = DatabaseBoundary(max_workers=2)
    gateway = ToolGateway()

    agent_version = get_agent_version(tenant_context, call.agent_version_id)

    async def scenario():
        return await gateway.execute(
            db=db,
            context=tenant_context,
            call_session_id=call.id,
            agent_version=agent_version,
            call_ref="ref",
            telephony=FakeTelephonyProvider(),
            system_service_account_name=SERVICE_ACCOUNT_NAME,
            request=ToolCallRequested(call_id="c1", name="call.hangup", arguments={}),
        )

    try:
        result = asyncio.run(scenario())
    finally:
        db.close()

    assert result.error_code == "unauthorized"


# --------------------------------------------------------------------------
# Full engine integration through run_call_task()
# --------------------------------------------------------------------------


def test_run_call_task_dispatches_a_real_tool_call_end_to_end(
    tenant_context, bootstrapped_service_account, system_actor_user_id
) -> None:
    call = _make_call(tenant_context, tools=["call.hangup"])
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    media = FakeMediaProvider()
    db = DatabaseBoundary(max_workers=4)
    engine = PipelinedEngine(
        FakeSttProvider(["hello"], frames_per_utterance=1),
        FakeLlmProvider(
            [[ToolCallRequested(call_id="c1", name="call.hangup", arguments={})], [TurnEnded()]]
        ),
        FakeTtsProvider(),
    )
    deps = CallTaskDependencies(
        engine=engine,
        media=media,
        telephony=telephony,
        db=db,
        policy_source=_permissive_policy_source(),
        tool_gateway=ToolGateway(),
        conversation_persistence=ConversationPersistence(db),
        system_actor_user_id=system_actor_user_id,
        system_service_account_name=bootstrapped_service_account,
    )
    cancellation = CancellationSignal()

    async def scenario() -> None:
        task = asyncio.create_task(
            run_call_task(
                context=tenant_context,
                call_session_id=call.id,
                call_ref=call_ref,
                deps=deps,
                cancellation=cancellation,
            )
        )
        for _ in range(200):
            if call_ref in media.streams:
                break
            await asyncio.sleep(0.01)
        media.streams[call_ref].push(b"frame")

        for _ in range(200):
            if ("hangup", call_ref, "normal") in telephony.commands:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("call.hangup was never dispatched through the real gateway")

        # Phase 2.5: wait for the durable "tool_result" turn too (not just
        # the telephony side effect above) before cancelling -- otherwise
        # cancellation could race ahead of ToolGateway.execute() actually
        # returning and cut off this turn's own persistence.
        for _ in range(200):
            turns = list_conversation_turns(tenant_context, call.id)
            if any(turn.role == "tool_result" for turn in turns):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the durable tool_result turn was never persisted")

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    try:
        asyncio.run(scenario())
    finally:
        db.close()

    assert call_ref not in telephony.live_calls
    entries = list_audit_log(tenant_context.tenant_id, resource_type="tool_call")
    assert any((entry.entry_metadata or {}).get("status") == "succeeded" for entry in entries)

    # Phase 2.5: the real Tool Gateway dispatch above also produced durable
    # conversation turns -- the assistant's tool-call turn ordered strictly
    # before its own tool-result turn (brief section 5), through the real
    # ConversationPersistence -> DatabaseBoundary -> PostgreSQL path, not a
    # fake.
    turns = list_conversation_turns(tenant_context, call.id)
    sequences = [turn.sequence for turn in turns]
    assert sequences == sorted(sequences)
    roles = [turn.role for turn in turns]
    assert "tool_call" in roles
    assert "tool_result" in roles
    tool_call_index = roles.index("tool_call")
    tool_result_index = roles.index("tool_result")
    assert tool_call_index < tool_result_index
    tool_call_turn = turns[tool_call_index]
    assert tool_call_turn.tool_payload == {"name": "call.hangup", "arguments": {}}
