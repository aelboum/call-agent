"""Real-PostgreSQL (+ real Row-Level Security) verification of Phase 2.22's
`CallOrchestrator`: authoritative inbound routing, idempotent `CallSession`
creation, authorization-before-media ordering, runtime ownership, and
concurrency -- against the real `app.inbound_call_routes` trigger/table
(migrations/0012), not a mock of it.

Requires a real PostgreSQL instance with SaaS-OS's own migrations and this
product's migrations already applied -- see `tests/integration/README.md`.
Excluded from the default `pytest` run (`pytest -m integration`).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from core.identity import create_user
from core.tenancy import create_tenant

from voiceagent.agents.config import AgentConfig
from voiceagent.agents.service import create_agent, create_draft_version, update_agent
from voiceagent.agents.service import publish_version as _publish_version
from voiceagent.calls.models import CallSession
from voiceagent.calls.service import (
    create_call_session,
    get_call_session,
    get_call_session_by_fs_channel_uuid,
    list_call_sessions,
    transition_call_session,
)
from voiceagent.config.settings import AiProviderSettings
from voiceagent.phone_numbers.service import register_phone_number, update_phone_number
from voiceagent.runtime.conversation_persistence import ConversationPersistence
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.runtime.fakes import FakeHeartbeatStore
from voiceagent.runtime.heartbeat import RuntimeHeartbeat
from voiceagent.runtime.orchestrator import CallOrchestrator
from voiceagent.runtime.privacy import StaticAiDataPolicySource
from voiceagent.runtime.supervisor import CallRuntime
from voiceagent.runtime.telephony_events import TelephonyEventRouter
from voiceagent.telephony.contracts import (
    CallDirection,
    CallEvent,
    CallEventType,
    TransportError,
)
from voiceagent.telephony.fakes import FakeMediaProvider, FakeTelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.tools.gateway import ToolGateway

pytestmark = pytest.mark.integration


def _config(**overrides: object) -> AgentConfig:
    payload: dict[str, object] = {
        "instructions": "Answer the phone.",
        "language": "en",
        "voice": {"provider": "fake", "voice_id": "v1"},
        "engine": {
            "kind": "pipelined",
            "stt": {"provider": "fake", "config": {}},
            "llm": {"provider": "fake", "model": "fake-model", "config": {}},
            "tts": {"provider": "fake", "config": {}},
        },
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


def _denying_policy_source() -> StaticAiDataPolicySource:
    return StaticAiDataPolicySource(AiProviderSettings(eligible_providers=("some-other-provider",)))


def _e164() -> str:
    return f"+1555{uuid.uuid4().int % 10**7:07d}"


class _AutoAnsweringTelephonyProvider(FakeTelephonyProvider):
    """A real FreeSWITCH channel reports `CHANNEL_ANSWER` shortly after
    `uuid_answer` succeeds -- this double emits the equivalent `ANSWERED`
    event synchronously right after `answer()` records its command, so
    tests never need an arbitrary sleep/poll to simulate it."""

    async def answer(self, call_ref: str) -> None:
        await super().answer(call_ref)
        self.emit(
            CallEvent(
                type=CallEventType.ANSWERED, call_ref=call_ref, direction=CallDirection.INBOUND
            )
        )


class _MediaUnavailableTelephonyProvider(_AutoAnsweringTelephonyProvider):
    """Simulates `start_media_stream()` itself failing (FreeSWITCH rejects
    `uuid_audio_stream`, or the command times out) -- Phase 2.22 brief
    section 14: "Media unavailable: call must reach a deterministic
    terminal/error state rather than hanging indefinitely." """

    async def start_media_stream(self, call_ref: str) -> None:  # type: ignore[override]
        raise TransportError("media stream unavailable (simulated)")


@pytest.fixture
def tenant_context() -> TenantContext:
    tenant = create_tenant(f"phase22-{uuid.uuid4().hex[:8]}")
    user = create_user()
    return TenantContext(tenant_id=tenant.id, actor_id=user.id, membership_id=uuid.uuid4())


@pytest.fixture
def system_actor_user_id() -> uuid.UUID:
    return create_user().id


@pytest.fixture
def db() -> Iterator[DatabaseBoundary]:
    boundary = DatabaseBoundary(max_workers=4)
    yield boundary
    boundary.close()


def _make_orchestrator(
    *,
    db: DatabaseBoundary,
    telephony: FakeTelephonyProvider,
    media: FakeMediaProvider,
    heartbeats: FakeHeartbeatStore,
    system_actor_user_id: uuid.UUID,
    policy_source: StaticAiDataPolicySource | None = None,
    instance_id: str = "runtime-a",
    capacity: int = 10,
) -> tuple[CallOrchestrator, CallRuntime, TelephonyEventRouter]:
    call_runtime = CallRuntime(
        instance_id=instance_id,
        address=f"{instance_id}:0",
        capacity=capacity,
        heartbeat_store=heartbeats,
    )
    router = TelephonyEventRouter(telephony)
    orchestrator = CallOrchestrator(
        db=db,
        call_runtime=call_runtime,
        telephony=telephony,
        media=media,
        telephony_events=router,
        heartbeat_store=heartbeats,
        policy_source=policy_source or _permissive_policy_source(),
        tool_gateway=ToolGateway(),
        conversation_persistence=ConversationPersistence(db),
        system_actor_user_id=system_actor_user_id,
        system_service_account_name="voiceagent-runtime",
        answer_timeout_seconds=2.0,
    )
    router.on_unrouted_offer = orchestrator.handle_unrouted_offer
    return orchestrator, call_runtime, router


async def _register_heartbeat(
    heartbeats: FakeHeartbeatStore, instance_id: str, *, load: int = 0
) -> None:
    await heartbeats.write(
        RuntimeHeartbeat(
            instance_id=instance_id,
            address=f"{instance_id}:0",
            capacity=10,
            current_load=load,
            last_heartbeat_epoch_seconds=0.0,
        ),
        ttl_seconds=60.0,
    )


async def _wait_until(predicate, *, timeout_seconds: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was never met within the timeout")


def test_valid_inbound_route_creates_and_starts_a_call(
    tenant_context, system_actor_user_id, db
) -> None:
    agent = create_agent(tenant_context, name="Agent")
    draft = create_draft_version(tenant_context, agent.id, config=_config())
    _publish_version(tenant_context, agent.id, draft.id)
    number = register_phone_number(tenant_context, e164=_e164(), agent_id=agent.id)

    async def scenario() -> str:
        telephony = _AutoAnsweringTelephonyProvider()
        media = FakeMediaProvider()
        heartbeats = FakeHeartbeatStore()
        await _register_heartbeat(heartbeats, "runtime-a")
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=media,
            heartbeats=heartbeats,
            system_actor_user_id=system_actor_user_id,
        )
        router_task = asyncio.create_task(router.run())
        try:
            call_ref = telephony.offer_inbound(from_number="+15550100", to_number=number.e164)
            await _wait_until(lambda: call_runtime.current_load > 0)
            return call_ref
        finally:
            router_task.cancel()
            for cs_id in list(call_runtime.owned_call_session_ids):
                await call_runtime.cancel_call(cs_id, reason="hangup")
            await asyncio.sleep(0.05)

    call_ref = asyncio.run(scenario())
    assert call_ref


def test_unknown_route_is_rejected_and_hung_up(tenant_context, system_actor_user_id, db) -> None:
    async def scenario() -> list[tuple[str, ...]]:
        telephony = _AutoAnsweringTelephonyProvider()
        media = FakeMediaProvider()
        heartbeats = FakeHeartbeatStore()
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=media,
            heartbeats=heartbeats,
            system_actor_user_id=system_actor_user_id,
        )
        router_task = asyncio.create_task(router.run())
        try:
            telephony.offer_inbound(from_number="+15550100", to_number="+19998887777")
            await _wait_until(lambda: any(c[0] == "hangup" for c in telephony.commands))
            return list(telephony.commands)
        finally:
            router_task.cancel()

    commands = asyncio.run(scenario())
    assert any(c[0] == "hangup" for c in commands)
    assert not any(c[0] == "answer" for c in commands)
    assert not any(c[0] == "start_media_stream" for c in commands)


def test_disabled_route_is_rejected_and_hung_up(tenant_context, system_actor_user_id, db) -> None:
    agent = create_agent(tenant_context, name="Agent")
    draft = create_draft_version(tenant_context, agent.id, config=_config())
    _publish_version(tenant_context, agent.id, draft.id)
    number = register_phone_number(tenant_context, e164=_e164(), agent_id=agent.id)
    update_phone_number(tenant_context, number.id, inbound_enabled=False)

    async def scenario() -> list[tuple[str, ...]]:
        telephony = _AutoAnsweringTelephonyProvider()
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=FakeMediaProvider(),
            heartbeats=FakeHeartbeatStore(),
            system_actor_user_id=system_actor_user_id,
        )
        router_task = asyncio.create_task(router.run())
        try:
            telephony.offer_inbound(from_number="+15550100", to_number=number.e164)
            await _wait_until(lambda: any(c[0] == "hangup" for c in telephony.commands))
            return list(telephony.commands)
        finally:
            router_task.cancel()

    commands = asyncio.run(scenario())
    assert any(c[0] == "hangup" for c in commands)
    assert not any(c[0] == "answer" for c in commands)


def test_route_with_no_agent_is_rejected(tenant_context, system_actor_user_id, db) -> None:
    number = register_phone_number(tenant_context, e164=_e164(), agent_id=None)

    async def scenario() -> list[tuple[str, ...]]:
        telephony = _AutoAnsweringTelephonyProvider()
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=FakeMediaProvider(),
            heartbeats=FakeHeartbeatStore(),
            system_actor_user_id=system_actor_user_id,
        )
        router_task = asyncio.create_task(router.run())
        try:
            telephony.offer_inbound(from_number="+15550100", to_number=number.e164)
            await _wait_until(lambda: any(c[0] == "hangup" for c in telephony.commands))
            return list(telephony.commands)
        finally:
            router_task.cancel()

    commands = asyncio.run(scenario())
    assert any(c[0] == "hangup" for c in commands)
    assert not any(c[0] == "answer" for c in commands)


def test_inactive_agent_route_is_rejected(tenant_context, system_actor_user_id, db) -> None:
    agent = create_agent(tenant_context, name="Agent")
    draft = create_draft_version(tenant_context, agent.id, config=_config())
    _publish_version(tenant_context, agent.id, draft.id)
    update_agent(tenant_context, agent.id, status="archived")
    number = register_phone_number(tenant_context, e164=_e164(), agent_id=agent.id)

    async def scenario() -> list[tuple[str, ...]]:
        telephony = _AutoAnsweringTelephonyProvider()
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=FakeMediaProvider(),
            heartbeats=FakeHeartbeatStore(),
            system_actor_user_id=system_actor_user_id,
        )
        router_task = asyncio.create_task(router.run())
        try:
            telephony.offer_inbound(from_number="+15550100", to_number=number.e164)
            await _wait_until(lambda: any(c[0] == "hangup" for c in telephony.commands))
            return list(telephony.commands)
        finally:
            router_task.cancel()

    commands = asyncio.run(scenario())
    assert any(c[0] == "hangup" for c in commands)
    assert not any(c[0] == "answer" for c in commands)


def test_agent_with_no_published_version_is_rejected(
    tenant_context, system_actor_user_id, db
) -> None:
    agent = create_agent(tenant_context, name="Agent")
    create_draft_version(tenant_context, agent.id, config=_config())
    number = register_phone_number(tenant_context, e164=_e164(), agent_id=agent.id)

    async def scenario() -> list[tuple[str, ...]]:
        telephony = _AutoAnsweringTelephonyProvider()
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=FakeMediaProvider(),
            heartbeats=FakeHeartbeatStore(),
            system_actor_user_id=system_actor_user_id,
        )
        router_task = asyncio.create_task(router.run())
        try:
            telephony.offer_inbound(from_number="+15550100", to_number=number.e164)
            await _wait_until(lambda: any(c[0] == "hangup" for c in telephony.commands))
            return list(telephony.commands)
        finally:
            router_task.cancel()

    commands = asyncio.run(scenario())
    assert any(c[0] == "hangup" for c in commands)
    assert not any(c[0] == "answer" for c in commands)


def test_authorization_denial_happens_before_media_activation(
    tenant_context, system_actor_user_id, db
) -> None:
    agent = create_agent(tenant_context, name="Agent")
    draft = create_draft_version(tenant_context, agent.id, config=_config())
    _publish_version(tenant_context, agent.id, draft.id)
    number = register_phone_number(tenant_context, e164=_e164(), agent_id=agent.id)

    async def scenario() -> list[tuple[str, ...]]:
        telephony = _AutoAnsweringTelephonyProvider()
        heartbeats = FakeHeartbeatStore()
        await _register_heartbeat(heartbeats, "runtime-a")
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=FakeMediaProvider(),
            heartbeats=heartbeats,
            system_actor_user_id=system_actor_user_id,
            policy_source=_denying_policy_source(),
        )
        router_task = asyncio.create_task(router.run())
        try:
            telephony.offer_inbound(from_number="+15550100", to_number=number.e164)
            await _wait_until(lambda: any(c[0] == "hangup" for c in telephony.commands))
            return list(telephony.commands)
        finally:
            router_task.cancel()

    commands = asyncio.run(scenario())
    assert any(c[0] == "hangup" for c in commands)
    assert not any(c[0] == "answer" for c in commands)
    assert not any(c[0] == "start_media_stream" for c in commands)


def test_no_runtime_capacity_is_rejected(tenant_context, system_actor_user_id, db) -> None:
    agent = create_agent(tenant_context, name="Agent")
    draft = create_draft_version(tenant_context, agent.id, config=_config())
    _publish_version(tenant_context, agent.id, draft.id)
    number = register_phone_number(tenant_context, e164=_e164(), agent_id=agent.id)

    async def scenario() -> list[tuple[str, ...]]:
        telephony = _AutoAnsweringTelephonyProvider()
        heartbeats = FakeHeartbeatStore()  # no runtime ever registers a heartbeat
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=FakeMediaProvider(),
            heartbeats=heartbeats,
            system_actor_user_id=system_actor_user_id,
        )
        router_task = asyncio.create_task(router.run())
        try:
            telephony.offer_inbound(from_number="+15550100", to_number=number.e164)
            await _wait_until(lambda: any(c[0] == "hangup" for c in telephony.commands))
            return list(telephony.commands)
        finally:
            router_task.cancel()

    commands = asyncio.run(scenario())
    assert any(c[0] == "hangup" for c in commands)
    assert not any(c[0] == "answer" for c in commands)


def test_media_unavailable_ends_the_call_deterministically(
    tenant_context, system_actor_user_id, db
) -> None:
    agent = create_agent(tenant_context, name="Agent")
    draft = create_draft_version(tenant_context, agent.id, config=_config())
    _publish_version(tenant_context, agent.id, draft.id)
    number = register_phone_number(tenant_context, e164=_e164(), agent_id=agent.id)

    async def scenario() -> tuple[list[tuple[str, ...]], CallSession | None]:
        telephony = _MediaUnavailableTelephonyProvider()
        heartbeats = FakeHeartbeatStore()
        await _register_heartbeat(heartbeats, "runtime-a")
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=FakeMediaProvider(),
            heartbeats=heartbeats,
            system_actor_user_id=system_actor_user_id,
        )
        router_task = asyncio.create_task(router.run())
        try:
            call_ref = telephony.offer_inbound(from_number="+15550100", to_number=number.e164)
            await _wait_until(lambda: any(c[0] == "hangup" for c in telephony.commands))
            call = get_call_session_by_fs_channel_uuid(tenant_context, call_ref)
            return list(telephony.commands), call
        finally:
            router_task.cancel()

    commands, call = asyncio.run(scenario())
    assert any(c[0] == "answer" for c in commands)
    assert any(c[0] == "hangup" for c in commands)
    assert call is not None
    assert call.status == "failed"
    assert call.end_reason == "media_unavailable"


def test_terminal_call_session_is_never_restarted(tenant_context, system_actor_user_id, db) -> None:
    agent = create_agent(tenant_context, name="Agent")
    draft = create_draft_version(tenant_context, agent.id, config=_config())
    version = _publish_version(tenant_context, agent.id, draft.id)
    number = register_phone_number(tenant_context, e164=_e164(), agent_id=agent.id)
    call_ref = f"terminal-{uuid.uuid4().hex[:8]}"
    call = create_call_session(
        tenant_context,
        direction="inbound",
        from_e164="+15550100",
        to_e164=number.e164,
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
        fs_channel_uuid=call_ref,
    )
    transition_call_session(tenant_context, call.id, to_status="failed", end_reason="test_setup")

    async def scenario() -> list[tuple[str, ...]]:
        telephony = _AutoAnsweringTelephonyProvider()
        heartbeats = FakeHeartbeatStore()
        await _register_heartbeat(heartbeats, "runtime-a")
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=FakeMediaProvider(),
            heartbeats=heartbeats,
            system_actor_user_id=system_actor_user_id,
        )
        router_task = asyncio.create_task(router.run())
        try:
            telephony.emit(
                CallEvent(
                    type=CallEventType.OFFERED,
                    call_ref=call_ref,
                    direction=CallDirection.INBOUND,
                    from_number="+15550100",
                    to_number=number.e164,
                )
            )
            telephony.live_calls.add(call_ref)
            await asyncio.sleep(0.2)
            return list(telephony.commands)
        finally:
            router_task.cancel()

    commands = asyncio.run(scenario())
    assert not any(c[0] == "answer" for c in commands)
    assert not any(c[0] == "hangup" for c in commands)
    refreshed = get_call_session(tenant_context, call.id)
    assert refreshed.status == "failed"


def test_duplicate_offered_events_for_the_same_call_start_exactly_once(
    tenant_context, system_actor_user_id, db
) -> None:
    agent = create_agent(tenant_context, name="Agent")
    draft = create_draft_version(tenant_context, agent.id, config=_config())
    _publish_version(tenant_context, agent.id, draft.id)
    number = register_phone_number(tenant_context, e164=_e164(), agent_id=agent.id)
    call_ref = f"dup-{uuid.uuid4().hex[:8]}"

    async def scenario() -> int:
        telephony = _AutoAnsweringTelephonyProvider()
        heartbeats = FakeHeartbeatStore()
        await _register_heartbeat(heartbeats, "runtime-a")
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=FakeMediaProvider(),
            heartbeats=heartbeats,
            system_actor_user_id=system_actor_user_id,
        )
        router_task = asyncio.create_task(router.run())
        try:
            telephony.live_calls.add(call_ref)
            offer = CallEvent(
                type=CallEventType.OFFERED,
                call_ref=call_ref,
                direction=CallDirection.INBOUND,
                from_number="+15550100",
                to_number=number.e164,
            )
            telephony.emit(offer)
            telephony.emit(offer)
            await _wait_until(lambda: call_runtime.current_load > 0)
            await asyncio.sleep(0.2)
            return len([c for c in telephony.commands if c[0] == "answer"])
        finally:
            router_task.cancel()
            for cs_id in list(call_runtime.owned_call_session_ids):
                await call_runtime.cancel_call(cs_id, reason="hangup")
            await asyncio.sleep(0.05)

    answer_count = asyncio.run(scenario())
    assert answer_count == 1


def test_two_runtimes_race_the_same_call_only_one_wins_ownership(
    tenant_context, system_actor_user_id, db
) -> None:
    agent = create_agent(tenant_context, name="Agent")
    draft = create_draft_version(tenant_context, agent.id, config=_config())
    _publish_version(tenant_context, agent.id, draft.id)
    number = register_phone_number(tenant_context, e164=_e164(), agent_id=agent.id)
    call_ref = f"race-{uuid.uuid4().hex[:8]}"

    async def scenario() -> tuple[int, int]:
        telephony_a = _AutoAnsweringTelephonyProvider()
        telephony_b = _AutoAnsweringTelephonyProvider()
        heartbeats = FakeHeartbeatStore()
        await _register_heartbeat(heartbeats, "runtime-a")
        await _register_heartbeat(heartbeats, "runtime-b")
        orchestrator_a, call_runtime_a, router_a = _make_orchestrator(
            db=db,
            telephony=telephony_a,
            media=FakeMediaProvider(),
            heartbeats=heartbeats,
            system_actor_user_id=system_actor_user_id,
            instance_id="runtime-a",
        )
        orchestrator_b, call_runtime_b, router_b = _make_orchestrator(
            db=db,
            telephony=telephony_b,
            media=FakeMediaProvider(),
            heartbeats=heartbeats,
            system_actor_user_id=system_actor_user_id,
            instance_id="runtime-b",
        )
        task_a = asyncio.create_task(router_a.run())
        task_b = asyncio.create_task(router_b.run())
        try:
            offer = CallEvent(
                type=CallEventType.OFFERED,
                call_ref=call_ref,
                direction=CallDirection.INBOUND,
                from_number="+15550100",
                to_number=number.e164,
            )
            telephony_a.live_calls.add(call_ref)
            telephony_b.live_calls.add(call_ref)
            telephony_a.emit(offer)
            telephony_b.emit(offer)
            await _wait_until(
                lambda: call_runtime_a.current_load > 0 or call_runtime_b.current_load > 0
            )
            await asyncio.sleep(0.2)
            return call_runtime_a.current_load, call_runtime_b.current_load
        finally:
            task_a.cancel()
            task_b.cancel()
            for rt in (call_runtime_a, call_runtime_b):
                for cs_id in list(rt.owned_call_session_ids):
                    await rt.cancel_call(cs_id, reason="hangup")
            await asyncio.sleep(0.05)

    load_a, load_b = asyncio.run(scenario())
    assert (load_a, load_b) in {(1, 0), (0, 1)}


def test_ten_concurrent_inbound_calls_do_not_cross_talk(
    tenant_context, system_actor_user_id, db
) -> None:
    agent = create_agent(tenant_context, name="Agent")
    draft = create_draft_version(tenant_context, agent.id, config=_config())
    _publish_version(tenant_context, agent.id, draft.id)
    number = register_phone_number(tenant_context, e164=_e164(), agent_id=agent.id)

    async def scenario() -> tuple[set[str], int]:
        telephony = _AutoAnsweringTelephonyProvider()
        heartbeats = FakeHeartbeatStore()
        await _register_heartbeat(heartbeats, "runtime-a", load=0)
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=FakeMediaProvider(),
            heartbeats=heartbeats,
            system_actor_user_id=system_actor_user_id,
            capacity=20,
        )
        router_task = asyncio.create_task(router.run())
        try:
            call_refs = {
                telephony.offer_inbound(from_number=f"+1555010{i}", to_number=number.e164)
                for i in range(10)
            }
            await _wait_until(lambda: call_runtime.current_load >= 10, timeout_seconds=10.0)
            return call_refs, len([c for c in telephony.commands if c[0] == "answer"])
        finally:
            router_task.cancel()
            for cs_id in list(call_runtime.owned_call_session_ids):
                await call_runtime.cancel_call(cs_id, reason="hangup")
            await asyncio.sleep(0.1)

    call_refs, answer_count = asyncio.run(scenario())
    assert len(call_refs) == 10
    assert answer_count == 10


def test_different_tenants_calling_simultaneously_stay_isolated(system_actor_user_id, db) -> None:
    tenant_1 = TenantContext(
        tenant_id=create_tenant(f"phase22-{uuid.uuid4().hex[:8]}").id,
        actor_id=create_user().id,
        membership_id=uuid.uuid4(),
    )
    tenant_2 = TenantContext(
        tenant_id=create_tenant(f"phase22-{uuid.uuid4().hex[:8]}").id,
        actor_id=create_user().id,
        membership_id=uuid.uuid4(),
    )

    def _setup(context: TenantContext):
        agent = create_agent(context, name="Agent")
        draft = create_draft_version(context, agent.id, config=_config())
        _publish_version(context, agent.id, draft.id)
        return register_phone_number(context, e164=_e164(), agent_id=agent.id)

    number_1 = _setup(tenant_1)
    number_2 = _setup(tenant_2)

    async def scenario() -> tuple[int, int]:
        telephony = _AutoAnsweringTelephonyProvider()
        heartbeats = FakeHeartbeatStore()
        await _register_heartbeat(heartbeats, "runtime-a", load=0)
        orchestrator, call_runtime, router = _make_orchestrator(
            db=db,
            telephony=telephony,
            media=FakeMediaProvider(),
            heartbeats=heartbeats,
            system_actor_user_id=system_actor_user_id,
            capacity=20,
        )
        router_task = asyncio.create_task(router.run())
        try:
            telephony.offer_inbound(from_number="+15550100", to_number=number_1.e164)
            telephony.offer_inbound(from_number="+15550200", to_number=number_2.e164)
            await _wait_until(lambda: call_runtime.current_load >= 2)
            calls_1 = len(list_call_sessions(tenant_1))
            calls_2 = len(list_call_sessions(tenant_2))
            return calls_1, calls_2
        finally:
            router_task.cancel()
            for cs_id in list(call_runtime.owned_call_session_ids):
                await call_runtime.cancel_call(cs_id, reason="hangup")
            await asyncio.sleep(0.1)

    calls_1, calls_2 = asyncio.run(scenario())
    assert calls_1 == 1
    assert calls_2 == 1
