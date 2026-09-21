"""Real-PostgreSQL (+ real database-backed `core.audit_log`) verification of
Phase 2.2's runtime substrate: runtime ownership, reconciliation, privacy
authorization, one full call lifecycle, and multi-call supervisor
concurrency (this phase's brief sections 13, 15, 20, 23, 27).

Requires a real PostgreSQL instance with SaaS-OS's own migrations and this
product's migrations already applied -- see `tests/integration/README.md`.
Excluded from the default `pytest` run (`pytest -m integration`).

Redis is deliberately NOT required here: every test uses
`voiceagent.runtime.fakes.FakeHeartbeatStore`, because ADR-0008 point 8's
liveness mechanism is Redis-shaped, not Redis-*only* -- `HeartbeatStore` is
the seam, and this file's own job is proving the runtime's logic, not
re-proving that `redis.asyncio` itself works.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from core.identity import create_user
from core.tenancy import create_tenant

from voiceagent.agents.config import AgentConfig
from voiceagent.agents.service import create_agent, create_draft_version, publish_version
from voiceagent.calls.errors import CallSessionAlreadyOwnedError
from voiceagent.calls.service import (
    claim_runtime_ownership,
    create_call_session,
    get_call_session,
    transition_call_session,
)
from voiceagent.config.settings import AiProviderSettings
from voiceagent.phone_numbers.service import register_phone_number
from voiceagent.providers.engines.component_fakes import (
    FakeLlmProvider,
    FakeSttProvider,
    FakeTtsProvider,
)
from voiceagent.providers.engines.contracts import EngineSessionConfig, TurnEnded
from voiceagent.providers.engines.pipelined import PipelinedEngine
from voiceagent.runtime.assignment import NoRuntimeCapacityError, assign_call_to_runtime
from voiceagent.runtime.call_task import CallTaskDependencies, CancellationSignal, run_call_task
from voiceagent.runtime.conversation_persistence import ConversationPersistence
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.runtime.errors import DataAuthorizationDeniedError
from voiceagent.runtime.fakes import FakeHeartbeatStore
from voiceagent.runtime.heartbeat import RuntimeHeartbeat
from voiceagent.runtime.privacy import StaticAiDataPolicySource, authorize_call_data_access
from voiceagent.runtime.reconciliation import reconcile_tenant
from voiceagent.runtime.supervisor import CallRuntime
from voiceagent.telephony.fakes import FakeMediaProvider, FakeTelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.tools.gateway import ToolGateway

pytestmark = pytest.mark.integration


def _config(**overrides) -> AgentConfig:
    payload = {
        "instructions": "Answer the phone.",
        "language": "en",
        "voice": {"provider": "fake", "voice_id": "v1"},
        "engine": {"kind": "pipelined", "stt": {"provider": "fake", "config": {}}},
        "business_hours": {"timezone": "UTC", "windows": []},
        "privacy": {"data_classification": "tenant_data", "purpose": "conversation"},
    }
    payload.update(overrides)
    return AgentConfig.model_validate(payload)


def _permissive_policy_source() -> StaticAiDataPolicySource:
    return StaticAiDataPolicySource(
        AiProviderSettings(
            eligible_providers=("fake",),
            allowed_data_classifications=("tenant_data",),
            allowed_purposes=("conversation",),
        )
    )


def _heartbeat(instance_id: str, *, load: int, capacity: int = 10) -> RuntimeHeartbeat:
    return RuntimeHeartbeat(
        instance_id=instance_id,
        address=f"ws://{instance_id}/media",
        capacity=capacity,
        current_load=load,
        last_heartbeat_epoch_seconds=0.0,
    )


@pytest.fixture
def tenant_context() -> TenantContext:
    tenant = create_tenant(f"phase22-{uuid.uuid4().hex[:8]}")
    user = create_user()
    return TenantContext(tenant_id=tenant.id, actor_id=user.id, membership_id=uuid.uuid4())


@pytest.fixture
def system_actor_user_id() -> uuid.UUID:
    """A real `core.users` row -- `authorize_data_access()`'s own
    `actor_user_id` foreign key requires one (`voiceagent.runtime.privacy`'s
    own module docstring, point 2)."""
    return create_user().id


@pytest.fixture
def system_service_account_name() -> str:
    """No test in this file emits a `ToolCallRequested` (none of Phase
    2.2's own runtime/lifecycle/concurrency tests drive the engine into a
    tool call), so `voiceagent.tools.gateway.ToolGateway.execute()` is never
    reached here and no real, bootstrapped, tenant-scoped service account is
    needed -- unlike `tests/integration/test_tool_gateway_integration.py`,
    which provisions a real one specifically to exercise that path. This is
    just `RuntimeSettings.system_service_account_name`'s own default,
    passed through `CallTaskDependencies` because the field is required."""
    return "voiceagent-runtime"


@pytest.fixture
def tool_gateway() -> ToolGateway:
    """A fresh `ToolGateway` per test -- its bounded idempotency cache is
    process-lifetime state in production, but must not leak between tests."""
    return ToolGateway()


@pytest.fixture
def make_call_session(tenant_context):
    """One published agent per test, and a factory for as many fresh
    `CallSession` rows against it as a test needs (distinct phone numbers,
    since `e164` is globally unique)."""
    agent = create_agent(tenant_context, name=f"Agent {uuid.uuid4().hex[:8]}")
    draft = create_draft_version(tenant_context, agent.id, config=_config())
    version = publish_version(tenant_context, agent.id, draft.id)

    def _make():
        number = register_phone_number(tenant_context, e164=f"+1555{uuid.uuid4().int % 10**7:07d}")
        return create_call_session(
            tenant_context,
            direction="inbound",
            from_e164="+15550100",
            to_e164=number.e164,
            phone_number_id=number.id,
            agent_id=agent.id,
            agent_version_id=version.id,
        )

    return tenant_context, _make


@pytest.fixture
def call_session(make_call_session):
    context, make = make_call_session
    return context, make()


# --------------------------------------------------------------------------
# Runtime ownership (brief section 13)
# --------------------------------------------------------------------------


def test_claim_runtime_ownership_is_idempotent_for_the_same_runtime(call_session) -> None:
    context, call = call_session
    first = claim_runtime_ownership(context, call.id, runtime_instance_id="runtime-a")
    second = claim_runtime_ownership(context, call.id, runtime_instance_id="runtime-a")
    assert second.runtime_instance_id == "runtime-a"
    assert second.runtime_assigned_at == first.runtime_assigned_at


def test_claim_runtime_ownership_rejects_a_different_runtime(call_session) -> None:
    context, call = call_session
    claim_runtime_ownership(context, call.id, runtime_instance_id="runtime-a")
    with pytest.raises(CallSessionAlreadyOwnedError):
        claim_runtime_ownership(context, call.id, runtime_instance_id="runtime-b")


def test_assign_call_to_runtime_selects_least_loaded_and_claims(call_session) -> None:
    context, call = call_session
    heartbeats = {"busy": _heartbeat("busy", load=9), "free": _heartbeat("free", load=0)}
    claimed, instance_id = assign_call_to_runtime(context, call.id, heartbeats)
    assert instance_id == "free"
    assert claimed.runtime_instance_id == "free"


def test_assign_call_to_runtime_raises_when_no_capacity(call_session) -> None:
    context, call = call_session
    heartbeats = {"full": _heartbeat("full", load=1, capacity=1)}
    with pytest.raises(NoRuntimeCapacityError):
        assign_call_to_runtime(context, call.id, heartbeats)


# --------------------------------------------------------------------------
# Reconciliation (brief section 15) -- no takeover, only bookkeeping repair
# --------------------------------------------------------------------------


def test_reconcile_tenant_marks_stale_ownership_interrupted(call_session) -> None:
    context, call = call_session
    transition_call_session(context, call.id, to_status="answered")
    transition_call_session(context, call.id, to_status="in_progress")
    claim_runtime_ownership(context, call.id, runtime_instance_id="crashed-runtime")

    report = reconcile_tenant(context, heartbeats={})

    assert call.id in [row.id for row in report.stale]
    assert call.id in [row.id for row in report.repaired]

    refreshed = get_call_session(context, call.id)
    assert refreshed.status == "interrupted"
    assert refreshed.end_reason == "runtime_crashed"


def test_reconcile_tenant_leaves_live_ownership_alone(call_session) -> None:
    context, call = call_session
    transition_call_session(context, call.id, to_status="answered")
    transition_call_session(context, call.id, to_status="in_progress")
    claim_runtime_ownership(context, call.id, runtime_instance_id="alive")

    report = reconcile_tenant(context, heartbeats={"alive": _heartbeat("alive", load=1)})

    assert report.stale == ()
    refreshed = get_call_session(context, call.id)
    assert refreshed.status == "in_progress"


def test_reconcile_tenant_is_idempotent(call_session) -> None:
    context, call = call_session
    transition_call_session(context, call.id, to_status="answered")
    transition_call_session(context, call.id, to_status="in_progress")
    claim_runtime_ownership(context, call.id, runtime_instance_id="crashed-runtime")

    reconcile_tenant(context, heartbeats={})
    second_report = reconcile_tenant(context, heartbeats={})

    assert second_report.stale == ()  # already terminal, no longer in the non-terminal scan


# --------------------------------------------------------------------------
# Privacy authorization (brief section 20)
# --------------------------------------------------------------------------


def test_authorize_call_data_access_allows_when_policy_permits(
    call_session, system_actor_user_id
) -> None:
    context, call = call_session
    decision = authorize_call_data_access(
        tenant_id=context.tenant_id,
        call_session_id=call.id,
        data_classification="tenant_data",
        purpose="conversation",
        provider="fake",
        policy_source=_permissive_policy_source(),
        system_actor_user_id=system_actor_user_id,
    )
    assert decision.outcome.value == "allow"


def test_authorize_call_data_access_denies_an_ineligible_provider(
    call_session, system_actor_user_id
) -> None:
    context, call = call_session
    restrictive = StaticAiDataPolicySource(AiProviderSettings(eligible_providers=("nothing",)))
    with pytest.raises(DataAuthorizationDeniedError):
        authorize_call_data_access(
            tenant_id=context.tenant_id,
            call_session_id=call.id,
            data_classification="tenant_data",
            purpose="conversation",
            provider="fake",
            policy_source=restrictive,
            system_actor_user_id=system_actor_user_id,
        )


# --------------------------------------------------------------------------
# One full call lifecycle (brief sections 17, 18, 20)
# --------------------------------------------------------------------------


def test_run_call_task_happy_path_completes_and_finalizes(
    call_session, system_actor_user_id
) -> None:
    context, call = call_session
    engine = PipelinedEngine(
        FakeSttProvider(["hello"], frames_per_utterance=1),
        FakeLlmProvider([[TurnEnded()]]),
        FakeTtsProvider(),
    )
    db = DatabaseBoundary(max_workers=2)
    deps = CallTaskDependencies(
        engine=engine,
        media=FakeMediaProvider(),
        telephony=FakeTelephonyProvider(),
        db=db,
        policy_source=_permissive_policy_source(),
        tool_gateway=ToolGateway(),
        conversation_persistence=ConversationPersistence(db),
        system_actor_user_id=system_actor_user_id,
        system_service_account_name="voiceagent-runtime",
    )
    cancellation = CancellationSignal()

    async def scenario() -> None:
        task = asyncio.create_task(
            run_call_task(
                context=context,
                call_session_id=call.id,
                call_ref="fake-call-1",
                deps=deps,
                cancellation=cancellation,
            )
        )
        await asyncio.sleep(0.1)
        assert get_call_session(context, call.id).status == "in_progress"
        task.cancel()  # the "hangup" a real TelephonyProvider event would trigger
        with contextlib.suppress(asyncio.CancelledError):
            await task

    try:
        asyncio.run(scenario())
    finally:
        db.close()

    refreshed = get_call_session(context, call.id)
    assert refreshed.status == "completed"
    assert refreshed.end_reason == "completed"


def test_run_call_task_denied_authorization_never_starts_the_engine(
    call_session, system_actor_user_id
) -> None:
    context, call = call_session

    class _FailIfStartedEngine:
        async def start(self, config: EngineSessionConfig):
            raise AssertionError("engine must never start after a denial")

    db = DatabaseBoundary(max_workers=2)
    deps = CallTaskDependencies(
        engine=_FailIfStartedEngine(),
        media=FakeMediaProvider(),
        telephony=FakeTelephonyProvider(),
        db=db,
        policy_source=StaticAiDataPolicySource(AiProviderSettings(eligible_providers=("other",))),
        tool_gateway=ToolGateway(),
        conversation_persistence=ConversationPersistence(db),
        system_actor_user_id=system_actor_user_id,
        system_service_account_name="voiceagent-runtime",
    )

    try:
        asyncio.run(
            run_call_task(
                context=context,
                call_session_id=call.id,
                call_ref="fake-call-2",
                deps=deps,
                cancellation=CancellationSignal(),
            )
        )
    finally:
        db.close()

    refreshed = get_call_session(context, call.id)
    assert refreshed.status == "failed"
    assert refreshed.end_reason == "authorization_denied"


# --------------------------------------------------------------------------
# Supervisor concurrency (brief section 23) and error isolation (section 27)
# --------------------------------------------------------------------------


def _deps(
    db: DatabaseBoundary,
    system_actor_user_id: uuid.UUID,
    system_service_account_name: str = "voiceagent-runtime",
    tool_gateway: ToolGateway | None = None,
) -> CallTaskDependencies:
    return CallTaskDependencies(
        engine=PipelinedEngine(
            FakeSttProvider(), FakeLlmProvider([[TurnEnded()]]), FakeTtsProvider()
        ),
        media=FakeMediaProvider(),
        telephony=FakeTelephonyProvider(),
        db=db,
        policy_source=_permissive_policy_source(),
        tool_gateway=tool_gateway if tool_gateway is not None else ToolGateway(),
        conversation_persistence=ConversationPersistence(db),
        system_actor_user_id=system_actor_user_id,
        system_service_account_name=system_service_account_name,
    )


def test_two_simultaneous_calls(make_call_session, system_actor_user_id) -> None:
    context, make = make_call_session
    call_a, call_b = make(), make()
    db = DatabaseBoundary(max_workers=4)
    runtime = CallRuntime(
        instance_id="rt-two", address="x", capacity=10, heartbeat_store=FakeHeartbeatStore()
    )

    async def scenario() -> None:
        await runtime.start_call(context, call_a.id, "ref-a", _deps(db, system_actor_user_id))
        await runtime.start_call(context, call_b.id, "ref-b", _deps(db, system_actor_user_id))
        await asyncio.sleep(0.1)
        assert runtime.current_load == 2
        await runtime.cancel_call(call_a.id, reason="hangup")
        await runtime.cancel_call(call_b.id, reason="hangup")
        assert runtime.current_load == 0

    try:
        asyncio.run(scenario())
    finally:
        db.close()

    assert get_call_session(context, call_a.id).status == "completed"
    assert get_call_session(context, call_b.id).status == "completed"


def test_ten_simultaneous_calls(make_call_session, system_actor_user_id) -> None:
    context, make = make_call_session
    calls = [make() for _ in range(10)]
    db = DatabaseBoundary(max_workers=8)
    runtime = CallRuntime(
        instance_id="rt-ten", address="x", capacity=20, heartbeat_store=FakeHeartbeatStore()
    )

    async def scenario() -> None:
        for index, call in enumerate(calls):
            deps = _deps(db, system_actor_user_id)
            await runtime.start_call(context, call.id, f"ref-{index}", deps)
        await asyncio.sleep(0.15)
        assert runtime.current_load == 10
        for call in calls:
            await runtime.cancel_call(call.id, reason="hangup")
        assert runtime.current_load == 0

    try:
        asyncio.run(scenario())
    finally:
        db.close()

    for call in calls:
        assert get_call_session(context, call.id).status == "completed"


def test_one_call_failing_does_not_affect_others(make_call_session, system_actor_user_id) -> None:
    class _BrokenEngine:
        async def start(self, config: EngineSessionConfig):
            raise RuntimeError("boom")

    context, make = make_call_session
    broken_call, healthy_call = make(), make()
    db = DatabaseBoundary(max_workers=4)
    runtime = CallRuntime(
        instance_id="rt-isolation", address="x", capacity=10, heartbeat_store=FakeHeartbeatStore()
    )
    broken_deps = CallTaskDependencies(
        engine=_BrokenEngine(),
        media=FakeMediaProvider(),
        telephony=FakeTelephonyProvider(),
        db=db,
        policy_source=_permissive_policy_source(),
        tool_gateway=ToolGateway(),
        conversation_persistence=ConversationPersistence(db),
        system_actor_user_id=system_actor_user_id,
        system_service_account_name="voiceagent-runtime",
    )

    async def scenario() -> None:
        await runtime.start_call(context, broken_call.id, "ref-broken", broken_deps)
        await runtime.start_call(
            context, healthy_call.id, "ref-healthy", _deps(db, system_actor_user_id)
        )
        await asyncio.sleep(0.1)
        assert runtime.error_for(broken_call.id) is not None
        assert runtime.is_running(healthy_call.id)
        await runtime.cancel_call(healthy_call.id, reason="hangup")

    try:
        asyncio.run(scenario())
    finally:
        db.close()

    assert get_call_session(context, healthy_call.id).status == "completed"


def test_cancelling_one_call_does_not_cancel_another(
    make_call_session, system_actor_user_id
) -> None:
    context, make = make_call_session
    call_a, call_b = make(), make()
    db = DatabaseBoundary(max_workers=4)
    runtime = CallRuntime(
        instance_id="rt-cancel", address="x", capacity=10, heartbeat_store=FakeHeartbeatStore()
    )

    async def scenario() -> None:
        await runtime.start_call(context, call_a.id, "ref-a", _deps(db, system_actor_user_id))
        await runtime.start_call(context, call_b.id, "ref-b", _deps(db, system_actor_user_id))
        await asyncio.sleep(0.05)
        await runtime.cancel_call(call_a.id, reason="hangup")
        assert not runtime.is_running(call_a.id)
        assert runtime.is_running(call_b.id)
        await runtime.cancel_call(call_b.id, reason="hangup")

    try:
        asyncio.run(scenario())
    finally:
        db.close()


def test_shutdown_cancels_all_owned_calls_and_deregisters_heartbeat(
    make_call_session, system_actor_user_id
) -> None:
    context, make = make_call_session
    call_a, call_b = make(), make()
    db = DatabaseBoundary(max_workers=4)
    heartbeat_store = FakeHeartbeatStore()
    runtime = CallRuntime(
        instance_id="rt-shutdown", address="x", capacity=10, heartbeat_store=heartbeat_store
    )

    async def scenario() -> None:
        await runtime.start_call(context, call_a.id, "ref-a", _deps(db, system_actor_user_id))
        await runtime.start_call(context, call_b.id, "ref-b", _deps(db, system_actor_user_id))
        runtime.start_heartbeat(interval_seconds=0.02, ttl_seconds=5)
        await asyncio.sleep(0.1)
        # Heartbeat stays alive while calls execute (brief section 23).
        assert "rt-shutdown" in await heartbeat_store.read_all()

        await runtime.shutdown()

        assert runtime.current_load == 0
        assert await heartbeat_store.read_all() == {}

    try:
        asyncio.run(scenario())
    finally:
        db.close()

    assert get_call_session(context, call_a.id).status == "interrupted"
    assert get_call_session(context, call_b.id).status == "interrupted"
    assert get_call_session(context, call_a.id).end_reason == "runtime_shutdown"
