"""`voiceagent.tools.gateway.ToolGateway` (Phase 2.4 brief section 14).

`core.rbac.can()` and `core.audit_log.record()` are monkeypatched at the
names `ToolGateway` actually calls them through (`voiceagent.tools.gateway
.rbac_can`/`.record_audit_event`) -- exactly the technique
`tests/runtime/test_privacy.py`'s own module docstring describes as the
alternative to needing a real database for a hermetic test of a function
that, in production, always crosses one. The real, DB-backed
allow/authorized-and-executes/denied/audited paths are proven against a real
PostgreSQL instance in
`tests/integration/test_tool_gateway_integration.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

import voiceagent.tools.handlers  # noqa: F401 -- registers the built-in tools
from voiceagent.agents.models import AgentVersion
from voiceagent.providers.engines.contracts import ToolCallRequested
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.telephony.fakes import FakeTelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.tools.definitions import StrictToolModel, ToolDefinition, ToolRisk
from voiceagent.tools.gateway import RESOURCE, ToolGateway
from voiceagent.tools.registry import ToolRegistry


@dataclass
class _AuditCall:
    action: str
    resource_id: str
    outcome: object
    metadata: dict[str, object]


class _AuditRecorder:
    def __init__(self) -> None:
        self.calls: list[_AuditCall] = []

    def __call__(self, **kwargs) -> None:
        self.calls.append(
            _AuditCall(
                action=kwargs["action"],
                resource_id=kwargs["resource_id"],
                outcome=kwargs["outcome"],
                metadata=kwargs["metadata"],
            )
        )

    def statuses(self) -> list[str]:
        return [str(call.metadata["status"]) for call in self.calls]


@pytest.fixture
def audit(monkeypatch) -> _AuditRecorder:
    recorder = _AuditRecorder()
    monkeypatch.setattr("voiceagent.tools.gateway.record_audit_event", recorder)
    return recorder


@pytest.fixture
def allow_authorization(monkeypatch):
    monkeypatch.setattr("voiceagent.tools.gateway.rbac_can", lambda **kwargs: True)


@pytest.fixture
def deny_authorization(monkeypatch):
    monkeypatch.setattr("voiceagent.tools.gateway.rbac_can", lambda **kwargs: False)


class _FakeServiceAccount:
    def __init__(self, account_id: uuid.UUID, name: str, status: str = "active") -> None:
        self.id = account_id
        self.name = name
        self.status = status


@pytest.fixture(autouse=True)
def resolve_service_account(monkeypatch):
    """By default, every tenant has exactly one active
    `voiceagent-runtime`-named service account -- most tests care about
    authorization/execution behavior, not resolution itself.
    `test_no_service_account_provisioned_for_this_tenant_fails_closed`
    overrides this directly."""
    account = _FakeServiceAccount(uuid.uuid4(), "voiceagent-runtime")
    monkeypatch.setattr(
        "voiceagent.tools.gateway.list_service_accounts", lambda tenant_id: [account]
    )


@pytest.fixture
def db() -> Iterator[DatabaseBoundary]:
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


class _NoArgsInput(StrictToolModel):
    pass


class _OkOutput(StrictToolModel):
    ok: bool = True


def _custom_registry(handler, *, timeout_seconds: float = 5.0) -> ToolRegistry:
    """A throwaway `ToolRegistry` carrying one tool, `test.custom`, built
    from `handler` -- used instead of mutating the process-wide
    `TOOL_REGISTRY`'s frozen `ToolDefinition`s, which `ToolDefinition`
    (`frozen=True, slots=True`) does not allow."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            tool_id="test.custom",
            name="test.custom",
            description="a throwaway tool for gateway execution tests",
            input_model=_NoArgsInput,
            output_model=_OkOutput,
            permission_action="test.custom",
            risk=ToolRisk.LOW,
            idempotent=True,
            timeout_seconds=timeout_seconds,
            handler=handler,
        )
    )
    return registry


def test_known_and_allowed_and_authorized_call_executes(
    db, context, audit, allow_authorization
) -> None:
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    agent_version = _agent_version(context, tools=["call.hangup"])
    gateway = ToolGateway()

    result = _run_execute(
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

    assert result.call_id == "c1"
    assert result.error_code is None
    assert result.value == {"hung_up": True}
    assert call_ref not in telephony.live_calls
    assert audit.statuses() == ["started", "succeeded"]


def test_unknown_tool_fails_closed_and_is_denied(db, context, audit, allow_authorization) -> None:
    gateway = ToolGateway()
    result = _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(context, tools=[]),
        call_ref="ref",
        telephony=FakeTelephonyProvider(),
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(call_id="c1", name="not.a.real.tool", arguments={}),
    )
    assert result.error_code == "unknown_tool"
    assert result.retryable is False
    assert audit.statuses() == ["denied"]


def test_tool_not_in_allowlist_is_denied(db, context, audit, allow_authorization) -> None:
    gateway = ToolGateway()
    result = _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(context, tools=["call.hold"]),  # not call.hangup
        call_ref="ref",
        telephony=FakeTelephonyProvider(),
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(call_id="c1", name="call.hangup", arguments={}),
    )
    assert result.error_code == "tool_not_allowed"
    assert audit.statuses() == ["denied"]


def test_unauthorized_principal_is_denied(db, context, audit, deny_authorization) -> None:
    gateway = ToolGateway()
    result = _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(context, tools=["call.hangup"]),
        call_ref="ref",
        telephony=FakeTelephonyProvider(),
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(call_id="c1", name="call.hangup", arguments={}),
    )
    assert result.error_code == "unauthorized"
    assert audit.statuses() == ["denied"]


def test_no_service_account_provisioned_for_this_tenant_fails_closed(
    db, context, audit, monkeypatch
) -> None:
    """No `voiceagent-runtime`-named `ACTIVE` service account exists yet for
    this tenant (an operator has not run `scripts/bootstrap_rbac.py` for it)
    -- fails closed as `unauthorized`, audited as `ActorType.SYSTEM` (no
    service account id to attribute to), never silently proceeds with no
    acting principal and never crashes."""
    monkeypatch.setattr("voiceagent.tools.gateway.list_service_accounts", lambda tenant_id: [])
    gateway = ToolGateway()
    result = _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(context, tools=["call.hangup"]),
        call_ref="ref",
        telephony=FakeTelephonyProvider(),
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(call_id="c1", name="call.hangup", arguments={}),
    )
    assert result.error_code == "unauthorized"
    assert audit.statuses() == ["denied"]
    assert audit.calls[0].outcome is not None


def test_malformed_input_is_a_validation_failure(db, context, audit, allow_authorization) -> None:
    gateway = ToolGateway()
    result = _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(context, tools=["call.transfer"]),
        call_ref="ref",
        telephony=FakeTelephonyProvider(),
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(
            call_id="c1", name="call.transfer", arguments={"destination_e164": "not-a-number"}
        ),
    )
    assert result.error_code == "invalid_arguments"
    assert audit.statuses() == ["validation_failed"]


def test_missing_required_field_is_a_validation_failure(
    db, context, audit, allow_authorization
) -> None:
    gateway = ToolGateway()
    result = _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(context, tools=["call.transfer"]),
        call_ref="ref",
        telephony=FakeTelephonyProvider(),
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(call_id="c1", name="call.transfer", arguments={}),
    )
    assert result.error_code == "invalid_arguments"


def test_a_tenant_id_supplied_in_arguments_is_never_used_and_is_rejected(
    db, context, audit, allow_authorization
) -> None:
    """Never accept a tenant/identity field the model supplied as authority
    (brief section 7). `StrictToolModel`'s `extra='forbid'` turns an
    attempted `tenant_id`/`call_session_id` argument into a validation
    failure -- proving there is no path through which it could be read."""
    gateway = ToolGateway()
    other_tenant = str(uuid.uuid4())
    result = _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(context, tools=["call.hangup"]),
        call_ref="ref",
        telephony=FakeTelephonyProvider(),
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(
            call_id="c1", name="call.hangup", arguments={"tenant_id": other_tenant}
        ),
    )
    assert result.error_code == "invalid_arguments"


def test_provider_error_normalization_on_handler_failure(
    db, context, audit, allow_authorization
) -> None:
    gateway = ToolGateway()
    telephony = FakeTelephonyProvider()  # call_ref never offered -> UnknownCallError
    result = _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(context, tools=["call.hangup"]),
        call_ref="never-offered",
        telephony=telephony,
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(call_id="c1", name="call.hangup", arguments={}),
    )
    assert result.error_code == "telephony_error"
    assert result.retryable is False
    assert audit.statuses() == ["started", "failed"]


def test_handler_timeout_is_normalized_and_retryable(
    db, context, audit, allow_authorization
) -> None:
    async def _hangs(ctx, tool_input):
        await asyncio.sleep(10)
        return {"ok": True}

    registry = _custom_registry(_hangs, timeout_seconds=0.01)
    gateway = ToolGateway(registry)
    result = _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(context, tools=["test.custom"]),
        call_ref="ref",
        telephony=FakeTelephonyProvider(),
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(call_id="c1", name="test.custom", arguments={}),
    )
    assert result.error_code == "timeout"
    assert result.retryable is True
    assert audit.statuses() == ["started", "timed_out"]


def test_cancellation_during_execution_propagates_and_is_audited(
    db, context, audit, allow_authorization
) -> None:
    started = asyncio.Event()

    async def _blocks_forever(ctx, tool_input):
        started.set()
        await asyncio.sleep(1000)
        return {"ok": True}  # pragma: no cover -- never reached

    registry = _custom_registry(_blocks_forever)
    gateway = ToolGateway(registry)

    async def scenario() -> None:
        task = asyncio.create_task(
            gateway.execute(
                db=db,
                context=context,
                call_session_id=uuid.uuid4(),
                agent_version=_agent_version(context, tools=["test.custom"]),
                call_ref="ref",
                telephony=FakeTelephonyProvider(),
                system_service_account_name="voiceagent-runtime",
                request=ToolCallRequested(call_id="c1", name="test.custom", arguments={}),
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert audit.statuses() == ["started", "cancelled"]


def test_malformed_handler_output_fails_closed(db, context, audit, allow_authorization) -> None:
    async def _returns_garbage(ctx, tool_input):
        return {"not_a_declared_field": True}

    registry = _custom_registry(_returns_garbage)
    gateway = ToolGateway(registry)
    result = _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(context, tools=["test.custom"]),
        call_ref="ref",
        telephony=FakeTelephonyProvider(),
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(call_id="c1", name="test.custom", arguments={}),
    )
    assert result.error_code == "internal_error"
    assert audit.statuses() == ["started", "failed"]


def test_duplicate_tool_call_id_is_answered_from_cache_without_reexecuting(
    db, context, audit, allow_authorization
) -> None:
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    agent_version = _agent_version(context, tools=["call.hangup"])
    gateway = ToolGateway()
    call_session_id = uuid.uuid4()
    request = ToolCallRequested(call_id="c1", name="call.hangup", arguments={})

    first = _run_execute(
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
    commands_after_first = list(telephony.commands)

    second = _run_execute(
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

    assert second == first
    # No second telephony command was issued for the replay.
    assert telephony.commands == commands_after_first
    assert audit.statuses() == ["started", "succeeded", "duplicate"]


def test_forget_call_clears_the_idempotency_cache_for_that_call(
    db, context, audit, allow_authorization
) -> None:
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    agent_version = _agent_version(context, tools=["call.hold"])
    gateway = ToolGateway()
    call_session_id = uuid.uuid4()
    request = ToolCallRequested(call_id="c1", name="call.hold", arguments={})

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
    gateway.forget_call(call_session_id)

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

    # Re-executed for real the second time (not answered from a cache that
    # should have been cleared) -- two "hold" commands were issued.
    assert [c for c in telephony.commands if c[0] == "hold"] == [
        ("hold", call_ref),
        ("hold", call_ref),
    ]


def test_forget_call_on_a_never_seen_call_is_a_harmless_no_op() -> None:
    ToolGateway().forget_call(uuid.uuid4())


def test_audit_metadata_never_carries_tool_arguments(
    db, context, audit, allow_authorization
) -> None:
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    gateway = ToolGateway()
    _run_execute(
        gateway,
        db=db,
        context=context,
        call_session_id=uuid.uuid4(),
        agent_version=_agent_version(context, tools=["call.transfer"]),
        call_ref=call_ref,
        telephony=telephony,
        system_service_account_name="voiceagent-runtime",
        request=ToolCallRequested(
            call_id="c1", name="call.transfer", arguments={"destination_e164": "+15559998888"}
        ),
    )
    for call in audit.calls:
        assert "destination_e164" not in call.metadata
        assert "+15559998888" not in str(call.metadata)


def test_gateway_resource_matches_permissions_module_resource() -> None:
    from voiceagent.tools.permissions import RESOURCE as PERMISSIONS_RESOURCE

    assert RESOURCE == PERMISSIONS_RESOURCE == "voiceagent.tools"
