"""Real-PostgreSQL verification of Phase 2.5's durable conversation model:
schema/RLS/tenant isolation, ordering, idempotency, deletion, the
`CallSession` relationship, and the API-layer read surface
(`voiceagent.api.v1.conversations`, `voiceagent.api.v1.call_sessions`'s own
pagination/filtering).

Requires a real PostgreSQL instance with SaaS-OS's own migrations and this
product's migrations already applied -- see `tests/integration/README.md`.
Excluded from the default `pytest` run (`pytest -m integration`).
"""

from __future__ import annotations

import uuid

import pytest
from core.identity import create_user
from core.tenancy import create_tenant

from voiceagent.agents.config import AgentConfig
from voiceagent.agents.service import create_agent, create_draft_version, publish_version
from voiceagent.api.v1.call_sessions import list_call_sessions_route
from voiceagent.api.v1.conversations import get_conversation_route
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.calls.service import create_call_session, transition_call_session
from voiceagent.conversations.errors import InvalidConversationTurnError
from voiceagent.conversations.models import ConversationTurn
from voiceagent.conversations.service import (
    delete_conversation_turns,
    list_conversation_turns,
    persist_conversation_turn,
)
from voiceagent.db import session_scope, tenant_session_scope
from voiceagent.phone_numbers.service import register_phone_number
from voiceagent.tenancy import TenantContext

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


@pytest.fixture
def tenant_context() -> TenantContext:
    tenant = create_tenant(f"phase25-{uuid.uuid4().hex[:8]}")
    user = create_user()
    return TenantContext(tenant_id=tenant.id, actor_id=user.id, membership_id=uuid.uuid4())


@pytest.fixture
def make_call_session(tenant_context):
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


@pytest.fixture
def two_tenant_calls(make_call_session):
    """A second, unrelated tenant/agent/call, for cross-tenant assertions."""
    context_a, make_a = make_call_session
    tenant_b = create_tenant(f"phase25-b-{uuid.uuid4().hex[:8]}")
    user_b = create_user()
    context_b = TenantContext(tenant_id=tenant_b.id, actor_id=user_b.id, membership_id=uuid.uuid4())
    agent_b = create_agent(context_b, name=f"Agent B {uuid.uuid4().hex[:8]}")
    draft_b = create_draft_version(context_b, agent_b.id, config=_config())
    version_b = publish_version(context_b, agent_b.id, draft_b.id)
    number_b = register_phone_number(context_b, e164=f"+1555{uuid.uuid4().int % 10**7:07d}")
    call_b = create_call_session(
        context_b,
        direction="inbound",
        from_e164="+15550100",
        to_e164=number_b.e164,
        phone_number_id=number_b.id,
        agent_id=agent_b.id,
        agent_version_id=version_b.id,
    )
    return (context_a, make_a()), (context_b, call_b)


# --------------------------------------------------------------------------
# Schema / RLS
# --------------------------------------------------------------------------


def test_conversation_turns_row_level_security_is_enabled_and_forced() -> None:
    with session_scope() as session:
        from sqlalchemy import text

        row = session.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relnamespace = 'app'::regnamespace AND relname = 'conversation_turns'"
            )
        ).one()
    assert row == (True, True)


def test_tenant_cannot_read_another_tenants_conversation_turn(call_session, tenant_context) -> None:
    context, call = call_session
    persist_conversation_turn(
        context, call.id, event_id="e1", role="user", content="hello", tool_payload=None
    )
    other_tenant = create_tenant(f"phase25-c-{uuid.uuid4().hex[:8]}")
    with tenant_session_scope(other_tenant.id) as session:
        rows = session.query(ConversationTurn).all()
    assert rows == []


def test_no_tenant_context_reads_no_conversation_turns(call_session) -> None:
    context, call = call_session
    persist_conversation_turn(
        context, call.id, event_id="e1", role="user", content="hello", tool_payload=None
    )
    with session_scope() as session:
        assert session.query(ConversationTurn).count() == 0


# --------------------------------------------------------------------------
# Ordering (brief section 5)
# --------------------------------------------------------------------------


def test_turns_are_assigned_a_monotonic_per_call_sequence(call_session) -> None:
    context, call = call_session
    first = persist_conversation_turn(
        context, call.id, event_id="e1", role="system", content="sys", tool_payload=None
    )
    second = persist_conversation_turn(
        context, call.id, event_id="e2", role="user", content="hi", tool_payload=None
    )
    third = persist_conversation_turn(
        context, call.id, event_id="e3", role="assistant", content="hello", tool_payload=None
    )
    assert first.sequence < second.sequence < third.sequence

    ordered = list_conversation_turns(context, call.id)
    assert [t.event_id for t in ordered] == ["e1", "e2", "e3"]


def test_tool_call_orders_before_its_tool_result(call_session) -> None:
    context, call = call_session
    persist_conversation_turn(
        context,
        call.id,
        event_id="tc-1",
        role="tool_call",
        content=None,
        tool_payload={"name": "call.hangup", "arguments": {}},
    )
    persist_conversation_turn(
        context,
        call.id,
        event_id="tc-1",
        role="tool_result",
        content=None,
        tool_payload={"value": {"ok": True}, "error_code": None},
    )
    turns = list_conversation_turns(context, call.id)
    roles = [t.role for t in turns]
    assert roles.index("tool_call") < roles.index("tool_result")


def test_sequences_stay_unique_and_gapless_under_many_turns(call_session) -> None:
    context, call = call_session
    for i in range(25):
        persist_conversation_turn(
            context, call.id, event_id=f"e{i}", role="user", content=str(i), tool_payload=None
        )
    turns = list_conversation_turns(context, call.id)
    sequences = [t.sequence for t in turns]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)


# --------------------------------------------------------------------------
# Idempotency (brief section 8)
# --------------------------------------------------------------------------


def test_duplicate_persistence_attempt_does_not_create_a_second_row(call_session) -> None:
    context, call = call_session
    first = persist_conversation_turn(
        context, call.id, event_id="e1", role="user", content="hi", tool_payload=None
    )
    second = persist_conversation_turn(
        context, call.id, event_id="e1", role="user", content="hi", tool_payload=None
    )
    assert first.id == second.id
    assert first.sequence == second.sequence
    assert len(list_conversation_turns(context, call.id)) == 1


def test_invalid_role_is_rejected(call_session) -> None:
    context, call = call_session
    with pytest.raises(InvalidConversationTurnError):
        persist_conversation_turn(
            context, call.id, event_id="e1", role="narrator", content="hi", tool_payload=None
        )


# --------------------------------------------------------------------------
# CallSession relationship / authorization boundary (brief sections 9, 12)
# --------------------------------------------------------------------------


def test_persisting_against_a_foreign_call_session_fails_closed(two_tenant_calls) -> None:
    (context_a, _call_a), (_context_b, call_b) = two_tenant_calls
    with pytest.raises(CallSessionNotFoundError):
        persist_conversation_turn(
            context_a, call_b.id, event_id="e1", role="user", content="hi", tool_payload=None
        )


def test_a_call_session_id_alone_is_not_sufficient_to_read_a_conversation(
    two_tenant_calls,
) -> None:
    (context_a, call_a), (context_b, _call_b) = two_tenant_calls
    persist_conversation_turn(
        context_a, call_a.id, event_id="e1", role="user", content="secret", tool_payload=None
    )
    with pytest.raises(CallSessionNotFoundError):
        list_conversation_turns(context_b, call_a.id)


def test_listing_a_call_with_no_conversation_yet_returns_an_empty_list(call_session) -> None:
    context, call = call_session
    assert list_conversation_turns(context, call.id) == []


# --------------------------------------------------------------------------
# Deletion / retention (brief section 10)
# --------------------------------------------------------------------------


def test_delete_conversation_turns_removes_every_row_for_the_call(call_session) -> None:
    context, call = call_session
    persist_conversation_turn(
        context, call.id, event_id="e1", role="user", content="hi", tool_payload=None
    )
    persist_conversation_turn(
        context, call.id, event_id="e2", role="assistant", content="hello", tool_payload=None
    )
    deleted = delete_conversation_turns(context, call.id)
    assert deleted == 2
    assert list_conversation_turns(context, call.id) == []


def test_delete_conversation_turns_for_an_empty_call_deletes_nothing(call_session) -> None:
    context, call = call_session
    assert delete_conversation_turns(context, call.id) == 0


def test_delete_conversation_turns_does_not_touch_another_calls_turns(make_call_session) -> None:
    context, make = make_call_session
    call_a, call_b = make(), make()
    persist_conversation_turn(
        context, call_a.id, event_id="e1", role="user", content="a", tool_payload=None
    )
    persist_conversation_turn(
        context, call_b.id, event_id="e1", role="user", content="b", tool_payload=None
    )
    delete_conversation_turns(context, call_a.id)
    assert list_conversation_turns(context, call_a.id) == []
    assert len(list_conversation_turns(context, call_b.id)) == 1


# --------------------------------------------------------------------------
# API read surface (brief section 11)
# --------------------------------------------------------------------------


def test_get_conversation_route_returns_ordered_turns(call_session) -> None:
    context, call = call_session
    persist_conversation_turn(
        context, call.id, event_id="e1", role="system", content="sys", tool_payload=None
    )
    persist_conversation_turn(
        context, call.id, event_id="e2", role="user", content="hi", tool_payload=None
    )
    out = get_conversation_route(call.id, context=context)
    assert [turn.role for turn in out] == ["system", "user"]
    assert [turn.content for turn in out] == ["sys", "hi"]


def test_get_conversation_route_404s_for_a_foreign_call(two_tenant_calls) -> None:
    from fastapi import HTTPException

    (context_a, call_a), (_context_b, _call_b) = two_tenant_calls
    other_tenant = create_tenant(f"phase25-d-{uuid.uuid4().hex[:8]}")
    other_context = TenantContext(
        tenant_id=other_tenant.id, actor_id=uuid.uuid4(), membership_id=uuid.uuid4()
    )
    with pytest.raises(HTTPException) as exc_info:
        get_conversation_route(call_a.id, context=other_context)
    assert exc_info.value.status_code == 404


def test_get_conversation_route_for_completed_call_with_no_turns_is_empty(call_session) -> None:
    context, call = call_session
    transition_call_session(context, call.id, to_status="answered")
    transition_call_session(context, call.id, to_status="in_progress")
    transition_call_session(context, call.id, to_status="completed")
    assert get_conversation_route(call.id, context=context) == []


def test_get_conversation_route_for_a_failed_call(call_session) -> None:
    context, call = call_session
    persist_conversation_turn(
        context, call.id, event_id="e1", role="system", content="sys", tool_payload=None
    )
    transition_call_session(context, call.id, to_status="failed", end_reason="authorization_denied")
    out = get_conversation_route(call.id, context=context)
    assert len(out) == 1


def test_list_call_sessions_route_paginates_and_filters_by_status(make_call_session) -> None:
    context, make = make_call_session
    calls = [make() for _ in range(3)]
    transition_call_session(context, calls[0].id, to_status="failed")

    all_calls = list_call_sessions_route(status=None, limit=200, offset=0, context=context)
    assert len(all_calls) >= 3

    first_page = list_call_sessions_route(status=None, limit=1, offset=0, context=context)
    second_page = list_call_sessions_route(status=None, limit=1, offset=1, context=context)
    assert len(first_page) == 1
    assert len(second_page) == 1
    assert first_page[0].id != second_page[0].id

    failed_only = list_call_sessions_route(status="failed", limit=200, offset=0, context=context)
    assert calls[0].id in {c.id for c in failed_only}
    assert all(c.status == "failed" for c in failed_only)


def test_list_call_sessions_route_with_an_unknown_status_returns_empty(call_session) -> None:
    context, _call = call_session
    result = list_call_sessions_route(
        status="not-a-real-status", limit=50, offset=0, context=context
    )
    assert result == []
