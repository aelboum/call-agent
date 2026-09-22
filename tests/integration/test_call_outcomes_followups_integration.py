"""Real-PostgreSQL verification of Phase 2.7 (Call Outcomes & Follow-up
Primitives): tenant isolation, cross-tenant call/outcome and
call/follow-up FK rejection, cross-tenant contact relationship rejection,
the one-outcome-per-call invariant, real Tool Gateway authorization, and
appointment-follow-up calendar integration (brief §22).

Requires a real PostgreSQL instance with SaaS-OS's own migrations and this
product's migrations (through `0005_call_outcomes_followups`) already
applied -- excluded from the default `pytest` run (`pytest -m integration`),
exactly mirroring `tests/integration/test_contacts_calendar_integration.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

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

from voiceagent.calendars.errors import CalendarEventConflictError
from voiceagent.calendars.service import create_calendar, create_event
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.calls.service import create_call_session
from voiceagent.contacts.errors import ContactNotFoundError
from voiceagent.contacts.service import create_contact
from voiceagent.db import IntegrityError, tenant_session_scope
from voiceagent.followups.errors import (
    CallOutcomeAlreadyExistsError,
    CallOutcomeNotFoundError,
    FollowUpActionNotFoundError,
    InvalidFollowUpTransitionError,
)
from voiceagent.followups.models import CallOutcome, FollowUpAction
from voiceagent.followups.service import (
    cancel_follow_up,
    complete_follow_up,
    create_call_outcome,
    create_follow_up,
    get_call_outcome,
    get_follow_up,
    set_call_outcome,
    update_call_outcome,
)
from voiceagent.providers.engines.contracts import ToolCallRequested
from voiceagent.rbac_bootstrap import PERMISSIONS, bootstrap_tenant_rbac
from voiceagent.telephony.fakes import FakeTelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.tools.gateway import ToolGateway
from voiceagent.tools.registry import TOOL_REGISTRY

pytestmark = pytest.mark.integration


def _aware(hour: int, day: int = 1, minute: int = 0) -> datetime:
    return datetime(2026, 12, day, hour, minute, tzinfo=UTC)


def _phone() -> str:
    return f"+1555{uuid.uuid4().int % 10**7:07d}"


@pytest.fixture(scope="module")
def two_tenants() -> tuple[TenantContext, TenantContext]:
    tenant_a = create_tenant(f"phase27-a-{uuid.uuid4().hex[:8]}")
    tenant_b = create_tenant(f"phase27-b-{uuid.uuid4().hex[:8]}")
    user_a = create_user()
    user_b = create_user()
    context_a = TenantContext(tenant_id=tenant_a.id, actor_id=user_a.id, membership_id=uuid.uuid4())
    context_b = TenantContext(tenant_id=tenant_b.id, actor_id=user_b.id, membership_id=uuid.uuid4())
    return context_a, context_b


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


# --------------------------------------------------------------------------
# CallOutcome: tenant isolation, one-per-call, cross-tenant FK (brief §22.1,3,5,6)
# --------------------------------------------------------------------------


def test_tenant_cannot_read_another_tenants_outcome(two_tenants) -> None:
    context_a, context_b = two_tenants
    call = _make_call(context_a)
    outcome = create_call_outcome(context_a, call.id, outcome="resolved")

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(CallOutcome, outcome.id) is None


def test_one_outcome_per_call_is_enforced(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    create_call_outcome(context_a, call.id, outcome="resolved")

    with pytest.raises(CallOutcomeAlreadyExistsError):
        create_call_outcome(context_a, call.id, outcome="no_answer")


def test_get_and_update_call_outcome(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    create_call_outcome(context_a, call.id, outcome="resolved", notes="first pass")

    fetched = get_call_outcome(context_a, call.id)
    assert fetched.outcome == "resolved"
    assert fetched.notes == "first pass"

    updated = update_call_outcome(context_a, call.id, outcome="follow_up_required")
    assert updated.outcome == "follow_up_required"
    assert updated.notes == "first pass"  # untouched by a partial update


def test_get_call_outcome_not_found(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    with pytest.raises(CallOutcomeNotFoundError):
        get_call_outcome(context_a, call.id)


def test_set_call_outcome_is_idempotent_create_or_update(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)

    first = set_call_outcome(context_a, call.id, outcome="resolved")
    second = set_call_outcome(context_a, call.id, outcome="resolved")
    assert first.id == second.id

    third = set_call_outcome(context_a, call.id, outcome="appointment_scheduled")
    assert third.id == first.id
    assert third.outcome == "appointment_scheduled"


def test_set_call_outcome_never_touches_call_session_status(two_tenants) -> None:
    from voiceagent.calls.service import get_call_session

    context_a, _ = two_tenants
    call = _make_call(context_a)
    before = get_call_session(context_a, call.id).status
    set_call_outcome(context_a, call.id, outcome="resolved")
    after = get_call_session(context_a, call.id).status
    assert before == after


def test_call_outcome_cannot_reference_another_tenants_call(two_tenants) -> None:
    context_a, context_b = two_tenants
    call_a = _make_call(context_a)

    with pytest.raises(IntegrityError):
        with tenant_session_scope(context_b.tenant_id) as session:
            session.add(
                CallOutcome(
                    tenant_id=context_b.tenant_id,
                    call_session_id=call_a.id,  # belongs to tenant A
                    outcome="resolved",
                )
            )
            session.flush()


def test_call_outcome_cannot_reference_another_tenants_contact(two_tenants) -> None:
    context_a, context_b = two_tenants
    call_b = _make_call(context_b)
    contact_a = create_contact(context_a, name="Ada", phone_e164=_phone())

    with pytest.raises(IntegrityError):
        with tenant_session_scope(context_b.tenant_id) as session:
            session.add(
                CallOutcome(
                    tenant_id=context_b.tenant_id,
                    call_session_id=call_b.id,
                    contact_id=contact_a.id,  # belongs to tenant A
                    outcome="resolved",
                )
            )
            session.flush()


def test_call_outcome_auto_derives_contact_from_the_call(two_tenants) -> None:
    from voiceagent.calls.service import associate_call

    context_a, _ = two_tenants
    call = _make_call(context_a)
    contact = create_contact(context_a, name="Ada", phone_e164=_phone())
    associate_call(context_a, call.id, contact.id)

    outcome = create_call_outcome(context_a, call.id, outcome="resolved")
    assert outcome.contact_id == contact.id


def test_create_call_outcome_rejects_a_call_from_a_different_tenant(two_tenants) -> None:
    context_a, context_b = two_tenants
    call_a = _make_call(context_a)
    with pytest.raises(CallSessionNotFoundError):
        create_call_outcome(context_b, call_a.id, outcome="resolved")


# --------------------------------------------------------------------------
# FollowUpAction: tenant isolation, FK isolation, transitions (brief §22.2,4,10)
# --------------------------------------------------------------------------


def test_tenant_cannot_read_another_tenants_follow_up(two_tenants) -> None:
    context_a, context_b = two_tenants
    call = _make_call(context_a)
    follow_up = create_follow_up(context_a, call.id, type="manual_follow_up")

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(FollowUpAction, follow_up.id) is None


def test_follow_up_cannot_reference_another_tenants_call(two_tenants) -> None:
    context_a, context_b = two_tenants
    call_a = _make_call(context_a)

    with pytest.raises(IntegrityError):
        with tenant_session_scope(context_b.tenant_id) as session:
            session.add(
                FollowUpAction(
                    tenant_id=context_b.tenant_id,
                    call_session_id=call_a.id,  # belongs to tenant A
                    type="manual_follow_up",
                    status="pending",
                )
            )
            session.flush()


def test_create_follow_up_rejects_a_call_from_a_different_tenant(two_tenants) -> None:
    context_a, context_b = two_tenants
    call_a = _make_call(context_a)
    with pytest.raises(CallSessionNotFoundError):
        create_follow_up(context_b, call_a.id, type="manual_follow_up")


def test_create_follow_up_rejects_a_contact_from_a_different_tenant(two_tenants) -> None:
    context_a, context_b = two_tenants
    call_b = _make_call(context_b)
    contact_a = create_contact(context_a, name="Ada", phone_e164=_phone())
    with pytest.raises(ContactNotFoundError):
        create_follow_up(context_b, call_b.id, type="manual_follow_up", contact_id=contact_a.id)


def test_follow_up_lifecycle_complete(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    follow_up = create_follow_up(
        context_a, call.id, type="manual_follow_up", description="ring back"
    )
    assert follow_up.status == "pending"

    completed = complete_follow_up(context_a, follow_up.id)
    assert completed.status == "completed"

    # Idempotent re-completion.
    completed_again = complete_follow_up(context_a, follow_up.id)
    assert completed_again.status == "completed"

    with pytest.raises(InvalidFollowUpTransitionError):
        cancel_follow_up(context_a, follow_up.id)


def test_follow_up_lifecycle_cancel(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    follow_up = create_follow_up(context_a, call.id, type="contact", description="send email")

    cancelled = cancel_follow_up(context_a, follow_up.id)
    assert cancelled.status == "cancelled"

    # Cancelled follow-ups remain durable -- still readable, not deleted.
    fetched = get_follow_up(context_a, follow_up.id)
    assert fetched.status == "cancelled"


def test_cancel_on_a_nonexistent_follow_up_is_not_found(two_tenants) -> None:
    context_a, _ = two_tenants
    with pytest.raises(FollowUpActionNotFoundError):
        cancel_follow_up(context_a, uuid.uuid4())


# --------------------------------------------------------------------------
# Appointment follow-up <-> Calendar integration (brief §9/§22.9)
# --------------------------------------------------------------------------


def test_appointment_follow_up_creates_the_calendar_event(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    calendar = create_calendar(context_a, name="Front Desk", timezone="UTC")

    follow_up = create_follow_up(
        context_a,
        call.id,
        type="appointment",
        calendar_id=calendar.id,
        start_at=_aware(10, 1),
        end_at=_aware(11, 1),
    )
    assert follow_up.calendar_event_id is not None

    with tenant_session_scope(context_a.tenant_id) as session:
        from voiceagent.calendars.models import CalendarEvent

        event = session.get(CalendarEvent, follow_up.calendar_event_id)
        assert event is not None
        assert event.calendar_id == calendar.id
        assert event.start_at == _aware(10, 1)


def test_appointment_follow_up_with_overlapping_interval_is_rejected(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    calendar = create_calendar(context_a, name="Busy Desk", timezone="UTC")
    create_event(
        context_a,
        calendar_id=calendar.id,
        title="Existing",
        start_at=_aware(10, 2),
        end_at=_aware(11, 2),
    )

    with pytest.raises(CalendarEventConflictError):
        create_follow_up(
            context_a,
            call.id,
            type="appointment",
            calendar_id=calendar.id,
            start_at=_aware(10, 2, minute=30),
            end_at=_aware(11, 2, minute=30),
        )


def test_appointment_follow_up_with_an_existing_calendar_event_is_accepted(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    calendar = create_calendar(context_a, name="Front Desk 2", timezone="UTC")
    event = create_event(
        context_a,
        calendar_id=calendar.id,
        title="Pre-booked",
        start_at=_aware(10, 3),
        end_at=_aware(11, 3),
    )

    follow_up = create_follow_up(context_a, call.id, type="appointment", calendar_event_id=event.id)
    assert follow_up.calendar_event_id == event.id


def test_appointment_follow_up_without_calendar_parameters_is_rejected(two_tenants) -> None:
    from voiceagent.followups.errors import FollowUpAppointmentRequiresCalendarEventError

    context_a, _ = two_tenants
    call = _make_call(context_a)
    with pytest.raises(FollowUpAppointmentRequiresCalendarEventError):
        create_follow_up(context_a, call.id, type="appointment")


# --------------------------------------------------------------------------
# Tool Gateway -> FollowUpService -> DB under real tenant authorization
# (brief §22.7,8)
# --------------------------------------------------------------------------

SERVICE_ACCOUNT_NAME = "voiceagent-runtime"


@pytest.fixture
def tenant_and_admin():
    tenant = create_tenant(f"phase27-tools-{uuid.uuid4().hex[:8]}")
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


def test_set_outcome_tool_executes_through_the_real_gateway(
    tenant_context, bootstrapped_service_account
) -> None:
    from voiceagent.runtime.db import DatabaseBoundary

    call = _make_call(tenant_context)
    from voiceagent.agents.service import get_agent_version

    agent_version = get_agent_version(tenant_context, call.agent_version_id)
    object.__setattr__(
        agent_version,
        "config",
        {**agent_version.config, "tools": [{"key": "call.set_outcome", "config": {}}]},
    )

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
            request=ToolCallRequested(
                call_id="c1", name="call.set_outcome", arguments={"outcome": "resolved"}
            ),
        )
    finally:
        db.close()

    assert result.error_code is None
    assert result.value["outcome"] == "resolved"
    assert result.value["call_session_id"] == str(call.id)


def test_create_follow_up_tool_executes_through_the_real_gateway(
    tenant_context, bootstrapped_service_account
) -> None:
    from voiceagent.runtime.db import DatabaseBoundary

    call = _make_call(tenant_context)
    from voiceagent.agents.service import get_agent_version

    agent_version = get_agent_version(tenant_context, call.agent_version_id)
    object.__setattr__(
        agent_version,
        "config",
        {**agent_version.config, "tools": [{"key": "call.create_follow_up", "config": {}}]},
    )

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
            request=ToolCallRequested(
                call_id="c1",
                name="call.create_follow_up",
                arguments={"type": "manual_follow_up", "description": "call back"},
            ),
        )
    finally:
        db.close()

    assert result.error_code is None
    assert result.value["follow_up"]["status"] == "pending"
    assert result.value["follow_up"]["call_session_id"] == str(call.id)


def test_follow_up_tool_not_in_allowlist_is_denied(
    tenant_context, bootstrapped_service_account
) -> None:
    from voiceagent.runtime.db import DatabaseBoundary

    call = _make_call(tenant_context)
    from voiceagent.agents.service import get_agent_version

    agent_version = get_agent_version(tenant_context, call.agent_version_id)  # tools: []

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
            request=ToolCallRequested(
                call_id="c1", name="call.set_outcome", arguments={"outcome": "resolved"}
            ),
        )
    finally:
        db.close()

    assert result.error_code == "tool_not_allowed"


def test_unauthorized_tenant_access_fails_closed(tenant_context) -> None:
    """Brief §22.8: no RBAC bootstrap has been run for this tenant/service
    account here -- the tool must fail closed as `unauthorized`, never
    silently execute."""
    from voiceagent.runtime.db import DatabaseBoundary

    call = _make_call(tenant_context)
    from voiceagent.agents.service import get_agent_version

    agent_version = get_agent_version(tenant_context, call.agent_version_id)
    object.__setattr__(
        agent_version,
        "config",
        {**agent_version.config, "tools": [{"key": "call.set_outcome", "config": {}}]},
    )

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
            request=ToolCallRequested(
                call_id="c1", name="call.set_outcome", arguments={"outcome": "resolved"}
            ),
        )
    finally:
        db.close()

    assert result.error_code == "unauthorized"
