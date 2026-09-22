"""Real-PostgreSQL verification of Phase 2.8 (Call Analysis foundation):
deterministic metric computation from real persisted
`call_sessions`/`conversation_turns`/`call_outcomes`/`follow_up_actions`
rows, idempotent rebuild (no duplicates), tenant isolation, cross-tenant FK
rejection, RLS under the restricted `saas_os_app` role, real RBAC
authorization, and the API route layer (called directly, exactly the
technique `tests/integration/test_conversation_integration.py`'s own
`get_conversation_route`/`list_call_sessions_route` tests already
establish).

Requires a real PostgreSQL instance with SaaS-OS's own migrations and this
product's migrations (through `0006_call_analysis`) already applied --
excluded from the default `pytest` run (`pytest -m integration`).
"""

from __future__ import annotations

import uuid

import pytest
from core.identity import add_tenant_membership, create_user
from core.rbac import (
    PrincipalType,
    RoleScope,
    assign_first_role_for_new_tenant,
    can,
    create_role,
    grant_permission,
    register_permission,
)
from core.tenancy import create_tenant
from fastapi import HTTPException

from voiceagent.call_analysis.errors import CallAnalysisNotFoundError
from voiceagent.call_analysis.models import CallAnalysis
from voiceagent.call_analysis.permissions import RESOURCE as CALL_ANALYSIS_RESOURCE
from voiceagent.call_analysis.service import build_call_analysis, get_call_analysis
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.calls.service import associate_call, create_call_session, transition_call_session
from voiceagent.contacts.service import create_contact
from voiceagent.conversations.service import persist_conversation_turn
from voiceagent.db import IntegrityError, tenant_session_scope
from voiceagent.followups.service import cancel_follow_up, create_follow_up
from voiceagent.rbac_bootstrap import PERMISSIONS, bootstrap_tenant_rbac
from voiceagent.tenancy import TenantContext

pytestmark = pytest.mark.integration


def _config(**overrides):
    from voiceagent.agents.config import AgentConfig

    payload = {
        "instructions": "Answer the phone.",
        "language": "en",
        "voice": {"provider": "fake", "voice_id": "v1"},
        "engine": {"kind": "pipelined", "stt": {"provider": "fake", "config": {}}},
        "business_hours": {"timezone": "UTC", "windows": []},
        "privacy": {"data_classification": "tenant_data", "purpose": "call_assistance"},
    }
    payload.update(overrides)
    return AgentConfig.model_validate(payload)


def _phone() -> str:
    return f"+1555{uuid.uuid4().int % 10**7:07d}"


def _make_call(context: TenantContext):
    from voiceagent.agents.service import create_agent, create_draft_version, publish_version
    from voiceagent.phone_numbers.service import register_phone_number

    agent = create_agent(context, name=f"Agent {uuid.uuid4().hex[:8]}")
    draft = create_draft_version(context, agent.id, config=_config())
    version = publish_version(context, agent.id, draft.id)
    number = register_phone_number(context, e164=_phone())
    return create_call_session(
        context,
        direction="inbound",
        from_e164="+15550000000",
        to_e164=number.e164,
        phone_number_id=number.id,
        agent_id=agent.id,
        agent_version_id=version.id,
    )


@pytest.fixture(scope="module")
def two_tenants() -> tuple[TenantContext, TenantContext]:
    tenant_a = create_tenant(f"phase28-a-{uuid.uuid4().hex[:8]}")
    tenant_b = create_tenant(f"phase28-b-{uuid.uuid4().hex[:8]}")
    user_a = create_user()
    user_b = create_user()
    context_a = TenantContext(tenant_id=tenant_a.id, actor_id=user_a.id, membership_id=uuid.uuid4())
    context_b = TenantContext(tenant_id=tenant_b.id, actor_id=user_b.id, membership_id=uuid.uuid4())
    return context_a, context_b


def _turn(context, call_id, *, event_id, role, content=None, tool_payload=None):
    return persist_conversation_turn(
        context,
        call_id,
        event_id=event_id,
        role=role,
        content=content,
        tool_payload=tool_payload,
    )


# --------------------------------------------------------------------------
# Deterministic metric computation
# --------------------------------------------------------------------------


def test_build_call_analysis_counts_turns_by_role(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    _turn(context_a, call.id, event_id="e1", role="system", content="be nice")
    _turn(context_a, call.id, event_id="e2", role="user", content="hi")
    _turn(context_a, call.id, event_id="e3", role="assistant", content="hello")
    _turn(context_a, call.id, event_id="e4", role="user", content="bye")

    analysis = build_call_analysis(context_a, call.id)

    assert analysis.turn_count == 4
    assert analysis.user_turn_count == 2
    assert analysis.assistant_turn_count == 1
    assert analysis.tool_call_count == 0
    assert analysis.tool_result_count == 0
    assert analysis.status == "ready"


def test_build_call_analysis_counts_tool_call_and_tool_result_turns(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    _turn(
        context_a,
        call.id,
        event_id="c1",
        role="tool_call",
        tool_payload={"name": "call.hold", "arguments": {}},
    )
    _turn(
        context_a,
        call.id,
        event_id="c1",
        role="tool_result",
        tool_payload={"value": {"held": True}, "error_code": None},
    )

    analysis = build_call_analysis(context_a, call.id)

    assert analysis.tool_call_count == 1
    assert analysis.tool_result_count == 1
    assert analysis.turn_count == 2


def test_build_call_analysis_detects_transfer(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    _turn(
        context_a,
        call.id,
        event_id="c1",
        role="tool_call",
        tool_payload={"name": "call.transfer", "arguments": {"destination_e164": "+15551112222"}},
    )

    analysis = build_call_analysis(context_a, call.id)

    assert analysis.had_transfer is True
    assert analysis.had_hold is False


def test_build_call_analysis_detects_hold(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    _turn(
        context_a,
        call.id,
        event_id="c1",
        role="tool_call",
        tool_payload={"name": "call.hold", "arguments": {}},
    )

    analysis = build_call_analysis(context_a, call.id)

    assert analysis.had_hold is True
    assert analysis.had_transfer is False


def test_build_call_analysis_with_no_turns_has_zero_counts_and_no_flags(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)

    analysis = build_call_analysis(context_a, call.id)

    assert analysis.turn_count == 0
    assert analysis.had_transfer is False
    assert analysis.had_hold is False


def test_build_call_analysis_duration_is_none_before_the_call_ends(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)

    analysis = build_call_analysis(context_a, call.id)

    assert analysis.duration_ms is None


def test_build_call_analysis_duration_matches_the_call_session_once_terminal(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    transition_call_session(context_a, call.id, to_status="answered")
    ended = transition_call_session(context_a, call.id, to_status="completed")
    assert ended.duration_ms is not None

    analysis = build_call_analysis(context_a, call.id)

    assert analysis.duration_ms == ended.duration_ms


def test_build_call_analysis_contact_associated_reflects_the_call(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    not_associated = build_call_analysis(context_a, call.id)
    assert not_associated.contact_associated is False

    contact = create_contact(context_a, name="Ada", phone_e164=_phone())
    associate_call(context_a, call.id, contact.id)

    associated = build_call_analysis(context_a, call.id)
    assert associated.contact_associated is True


def test_build_call_analysis_outcome_is_a_snapshot(two_tenants) -> None:
    from voiceagent.followups.service import set_call_outcome

    context_a, _ = two_tenants
    call = _make_call(context_a)
    no_outcome_yet = build_call_analysis(context_a, call.id)
    assert no_outcome_yet.outcome is None

    set_call_outcome(context_a, call.id, outcome="resolved")
    with_outcome = build_call_analysis(context_a, call.id)
    assert with_outcome.outcome == "resolved"


def test_build_call_analysis_follow_up_counts(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    f1 = create_follow_up(context_a, call.id, type="manual_follow_up")
    create_follow_up(context_a, call.id, type="contact")
    create_follow_up(context_a, call.id, type="manual_follow_up")
    cancel_follow_up(context_a, f1.id)

    analysis = build_call_analysis(context_a, call.id)

    assert analysis.follow_up_count == 3
    assert analysis.appointment_follow_up_count == 0
    # f1 is cancelled (terminal) -- only the other two remain "open".
    assert analysis.open_follow_up_count == 2


def test_build_call_analysis_appointment_follow_up_count(two_tenants) -> None:
    from voiceagent.calendars.service import create_calendar

    context_a, _ = two_tenants
    call = _make_call(context_a)
    calendar = create_calendar(context_a, name="Front Desk", timezone="UTC")
    from datetime import UTC, datetime

    create_follow_up(
        context_a,
        call.id,
        type="appointment",
        calendar_id=calendar.id,
        start_at=datetime(2026, 12, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 12, 1, 11, tzinfo=UTC),
    )
    create_follow_up(context_a, call.id, type="manual_follow_up")

    analysis = build_call_analysis(context_a, call.id)

    assert analysis.appointment_follow_up_count == 1
    assert analysis.follow_up_count == 2


# --------------------------------------------------------------------------
# Idempotent rebuild (no duplicates)
# --------------------------------------------------------------------------


def test_rebuild_does_not_create_a_duplicate_row(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)

    first = build_call_analysis(context_a, call.id)
    second = build_call_analysis(context_a, call.id)

    assert first.id == second.id
    with tenant_session_scope(context_a.tenant_id) as session:
        from voiceagent.db import select

        rows = (
            session.execute(select(CallAnalysis).where(CallAnalysis.call_session_id == call.id))
            .scalars()
            .all()
        )
        assert len(rows) == 1


def test_rebuild_refreshes_derived_values(two_tenants) -> None:
    from voiceagent.followups.service import set_call_outcome

    context_a, _ = two_tenants
    call = _make_call(context_a)
    before = build_call_analysis(context_a, call.id)
    assert before.turn_count == 0
    assert before.outcome is None

    _turn(context_a, call.id, event_id="e1", role="user", content="hi")
    set_call_outcome(context_a, call.id, outcome="follow_up_required")

    after = build_call_analysis(context_a, call.id)
    assert after.id == before.id
    assert after.turn_count == 1
    assert after.outcome == "follow_up_required"


def test_build_call_analysis_rejects_a_call_from_a_different_tenant(two_tenants) -> None:
    context_a, context_b = two_tenants
    call_a = _make_call(context_a)
    with pytest.raises(CallSessionNotFoundError):
        build_call_analysis(context_b, call_a.id)


# --------------------------------------------------------------------------
# Tenant isolation / RLS / FK integrity
# --------------------------------------------------------------------------


def test_tenant_cannot_read_another_tenants_analysis(two_tenants) -> None:
    context_a, context_b = two_tenants
    call = _make_call(context_a)
    analysis = build_call_analysis(context_a, call.id)

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(CallAnalysis, analysis.id) is None


def test_call_analysis_cannot_reference_another_tenants_call(two_tenants) -> None:
    context_a, context_b = two_tenants
    call_a = _make_call(context_a)

    with pytest.raises(IntegrityError):
        with tenant_session_scope(context_b.tenant_id) as session:
            session.add(
                CallAnalysis(
                    tenant_id=context_b.tenant_id,
                    call_session_id=call_a.id,  # belongs to tenant A
                )
            )
            session.flush()


def test_get_call_analysis_not_found_before_any_build(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    with pytest.raises(CallAnalysisNotFoundError):
        get_call_analysis(context_a, call.id)


# --------------------------------------------------------------------------
# API route layer (called directly, per test_conversation_integration.py's
# own established technique)
# --------------------------------------------------------------------------


def test_get_call_analysis_route_returns_the_built_analysis(two_tenants) -> None:
    from voiceagent.api.v1.call_sessions import get_call_analysis_route

    context_a, _ = two_tenants
    call = _make_call(context_a)
    build_call_analysis(context_a, call.id)

    out = get_call_analysis_route(call.id, context=context_a)
    assert out.call_session_id == call.id
    assert out.status == "ready"


def test_get_call_analysis_route_404s_when_no_analysis_exists(two_tenants) -> None:
    from voiceagent.api.v1.call_sessions import get_call_analysis_route

    context_a, _ = two_tenants
    call = _make_call(context_a)

    with pytest.raises(HTTPException) as exc_info:
        get_call_analysis_route(call.id, context=context_a)
    assert exc_info.value.status_code == 404


def test_get_call_analysis_route_404s_for_a_foreign_call(two_tenants) -> None:
    from voiceagent.api.v1.call_sessions import get_call_analysis_route

    context_a, context_b = two_tenants
    call_a = _make_call(context_a)
    build_call_analysis(context_a, call_a.id)

    with pytest.raises(HTTPException) as exc_info:
        get_call_analysis_route(call_a.id, context=context_b)
    assert exc_info.value.status_code == 404


def test_rebuild_call_analysis_route_builds_and_returns(two_tenants) -> None:
    from voiceagent.api.v1.call_sessions import rebuild_call_analysis_route

    context_a, _ = two_tenants
    call = _make_call(context_a)
    _turn(context_a, call.id, event_id="e1", role="user", content="hi")

    out = rebuild_call_analysis_route(call.id, context=context_a)
    assert out.turn_count == 1


def test_rebuild_call_analysis_route_404s_for_a_foreign_call(two_tenants) -> None:
    from voiceagent.api.v1.call_sessions import rebuild_call_analysis_route

    context_a, context_b = two_tenants
    call_a = _make_call(context_a)

    with pytest.raises(HTTPException) as exc_info:
        rebuild_call_analysis_route(call_a.id, context=context_b)
    assert exc_info.value.status_code == 404


# --------------------------------------------------------------------------
# Real RBAC authorization
# --------------------------------------------------------------------------


@pytest.fixture
def tenant_and_admin():
    tenant = create_tenant(f"phase28-rbac-{uuid.uuid4().hex[:8]}")
    admin = create_user()
    membership = add_tenant_membership(tenant.id, admin.id)
    role = create_role(tenant.id, f"bootstrap-admin-{uuid.uuid4().hex[:8]}")
    for resource, action in (*PERMISSIONS, ("service_account_role", "create")):
        permission = register_permission(resource, action)
        grant_permission(tenant.id, role.id, permission.id)
    assign_first_role_for_new_tenant(tenant.id, membership.id, role.id, scope=RoleScope.SELF)
    return tenant.id, admin.id


def test_bootstrap_grants_call_analysis_read_and_rebuild(tenant_and_admin) -> None:
    tenant_id, admin_id = tenant_and_admin
    result = bootstrap_tenant_rbac(tenant_id=tenant_id, actor_user_id=admin_id)
    assert (CALL_ANALYSIS_RESOURCE, "read") in result.granted
    assert (CALL_ANALYSIS_RESOURCE, "rebuild") in result.granted


def test_an_unbootstrapped_actor_cannot_read_call_analysis(tenant_and_admin) -> None:
    """No grant for this resource/action exists for a brand-new user --
    `core.rbac.can()` must deny, never fail open."""
    tenant_id, _ = tenant_and_admin
    stranger = create_user()
    assert (
        can(
            actor_id=stranger.id,
            tenant_id=tenant_id,
            action="read",
            resource=CALL_ANALYSIS_RESOURCE,
            actor_type=PrincipalType.USER,
        )
        is False
    )
