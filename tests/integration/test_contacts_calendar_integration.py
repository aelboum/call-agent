"""Real-PostgreSQL verification of Phase 2.6 (Contacts + minimal internal
calendar): tenant isolation, cross-tenant call/contact association
rejection, tenant-local duplicate-phone behavior, calendar/event FK
isolation, RLS under the restricted `saas_os_app` role, overlap/adjacency
semantics, cancelled-event availability, and the Tool Gateway ->
service -> DB path under real tenant authorization (brief §21).

Requires a real PostgreSQL instance with SaaS-OS's own migrations and this
product's migrations (through `0004_contacts_calendar`) already applied --
excluded from the default `pytest` run (`pytest -m integration`), exactly
mirroring `tests/integration/test_domain_rls_integration.py` and
`tests/integration/test_tool_gateway_integration.py`.
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

from voiceagent.calendars.errors import (
    CalendarEventConflictError,
    CalendarNotFoundError,
)
from voiceagent.calendars.models import Calendar, CalendarEvent
from voiceagent.calendars.service import (
    cancel_event,
    check_availability,
    create_calendar,
    create_event,
    list_events,
)
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.calls.service import associate_call, create_call_session, get_call_session
from voiceagent.contacts.errors import ContactNotFoundError, ContactPhoneConflictError
from voiceagent.contacts.models import Contact
from voiceagent.contacts.service import create_contact, list_contacts, lookup_contact_by_phone
from voiceagent.db import IntegrityError, tenant_session_scope
from voiceagent.providers.engines.contracts import ToolCallRequested
from voiceagent.rbac_bootstrap import PERMISSIONS, bootstrap_tenant_rbac
from voiceagent.telephony.fakes import FakeTelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.tools.gateway import ToolGateway
from voiceagent.tools.registry import TOOL_REGISTRY

pytestmark = pytest.mark.integration


def _aware(hour: int, day: int = 1, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def two_tenants() -> tuple[TenantContext, TenantContext]:
    tenant_a = create_tenant(f"phase26-a-{uuid.uuid4().hex[:8]}")
    tenant_b = create_tenant(f"phase26-b-{uuid.uuid4().hex[:8]}")
    user_a = create_user()
    user_b = create_user()
    context_a = TenantContext(tenant_id=tenant_a.id, actor_id=user_a.id, membership_id=uuid.uuid4())
    context_b = TenantContext(tenant_id=tenant_b.id, actor_id=user_b.id, membership_id=uuid.uuid4())
    return context_a, context_b


def _phone() -> str:
    return f"+1555{uuid.uuid4().int % 10**7:07d}"


# --------------------------------------------------------------------------
# Contacts: tenant isolation + tenant-local duplicate phone (brief §21.1,5)
# --------------------------------------------------------------------------


def test_tenant_cannot_read_another_tenants_contact(two_tenants) -> None:
    context_a, context_b = two_tenants
    contact = create_contact(context_a, name="Ada Lovelace", phone_e164=_phone())

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(Contact, contact.id) is None


def test_duplicate_phone_within_same_tenant_is_rejected(two_tenants) -> None:
    context_a, _ = two_tenants
    phone = _phone()
    create_contact(context_a, name="First", phone_e164=phone)

    with pytest.raises(ContactPhoneConflictError):
        create_contact(context_a, name="Second", phone_e164=phone)


def test_same_phone_is_allowed_in_different_tenants(two_tenants) -> None:
    """The deliberate opposite of `PhoneNumber.e164` -- a contact's phone
    number carries no platform routing meaning."""
    context_a, context_b = two_tenants
    phone = _phone()
    contact_a = create_contact(context_a, name="Tenant A's Ada", phone_e164=phone)
    contact_b = create_contact(context_b, name="Tenant B's Ada", phone_e164=phone)
    assert contact_a.id != contact_b.id
    assert contact_a.phone_e164 == contact_b.phone_e164 == phone


def test_lookup_by_phone_never_returns_another_tenants_contact(two_tenants) -> None:
    context_a, context_b = two_tenants
    phone = _phone()
    create_contact(context_a, name="Ada", phone_e164=phone)
    assert lookup_contact_by_phone(context_b, phone) is None
    assert lookup_contact_by_phone(context_a, phone) is not None


def test_list_contacts_is_tenant_scoped(two_tenants) -> None:
    """Phase 2.15: `list_contacts()`'s first API-wired caller
    (`GET /v1/contacts`). Never returns another tenant's rows."""
    context_a, context_b = two_tenants
    contact_a = create_contact(context_a, name="List Test A", phone_e164=_phone())
    contact_b = create_contact(context_b, name="List Test B", phone_e164=_phone())
    ids_a = {contact.id for contact in list_contacts(context_a)}
    ids_b = {contact.id for contact in list_contacts(context_b)}
    assert contact_a.id in ids_a
    assert contact_a.id not in ids_b
    assert contact_b.id in ids_b
    assert contact_b.id not in ids_a


def test_list_contacts_respects_limit_and_offset(two_tenants) -> None:
    context_a, _ = two_tenants
    created = [create_contact(context_a, name=f"Paged {i}", phone_e164=_phone()) for i in range(3)]
    first_page = list_contacts(context_a, limit=1)
    assert len(first_page) == 1
    all_rows = list_contacts(context_a, limit=100)
    returned_ids = {row.id for row in all_rows}
    assert all(contact.id in returned_ids for contact in created)


# --------------------------------------------------------------------------
# Calendars: FK isolation, tenant isolation (brief §21.2,6)
# --------------------------------------------------------------------------


def test_tenant_cannot_read_another_tenants_calendar(two_tenants) -> None:
    context_a, context_b = two_tenants
    calendar = create_calendar(context_a, name="Front Desk", timezone="Europe/Amsterdam")

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(Calendar, calendar.id) is None


def test_tenant_cannot_read_another_tenants_appointment(two_tenants) -> None:
    context_a, context_b = two_tenants
    calendar = create_calendar(context_a, name="Front Desk", timezone="UTC")
    event = create_event(
        context_a,
        calendar_id=calendar.id,
        title="Checkup",
        start_at=_aware(10, 2),
        end_at=_aware(
            11,
            2,
        ),
    )

    with tenant_session_scope(context_b.tenant_id) as session:
        assert session.get(CalendarEvent, event.id) is None


def test_calendar_event_cannot_reference_another_tenants_calendar(two_tenants) -> None:
    context_a, context_b = two_tenants
    calendar_a = create_calendar(context_a, name="Front Desk", timezone="UTC")

    with pytest.raises(IntegrityError):
        with tenant_session_scope(context_b.tenant_id) as session:
            session.add(
                CalendarEvent(
                    tenant_id=context_b.tenant_id,
                    calendar_id=calendar_a.id,  # belongs to tenant A
                    title="Hijack",
                    start_at=_aware(10, 3),
                    end_at=_aware(11, 3),
                )
            )
            session.flush()


def test_calendar_event_cannot_reference_another_tenants_contact(two_tenants) -> None:
    context_a, context_b = two_tenants
    calendar_b = create_calendar(context_b, name="Front Desk", timezone="UTC")
    contact_a = create_contact(context_a, name="Ada", phone_e164=_phone())

    with pytest.raises(IntegrityError):
        with tenant_session_scope(context_b.tenant_id) as session:
            session.add(
                CalendarEvent(
                    tenant_id=context_b.tenant_id,
                    calendar_id=calendar_b.id,
                    contact_id=contact_a.id,  # belongs to tenant A
                    title="Hijack",
                    start_at=_aware(10, 4),
                    end_at=_aware(11, 4),
                )
            )
            session.flush()


def test_create_event_with_cross_tenant_contact_id_is_rejected_by_the_service(two_tenants) -> None:
    """The application-service-level equivalent of the FK test above: a
    `contact_id` belonging to a different tenant is invisible under RLS, so
    `create_event()` raises the identical `ContactNotFoundError` a
    same-tenant nonexistent id would (brief §16: no cross-tenant lookup
    mechanism)."""
    context_a, context_b = two_tenants
    calendar_b = create_calendar(context_b, name="Front Desk", timezone="UTC")
    contact_a = create_contact(context_a, name="Ada", phone_e164=_phone())

    with pytest.raises(ContactNotFoundError):
        create_event(
            context_b,
            calendar_id=calendar_b.id,
            title="Hijack",
            start_at=_aware(10, 5),
            end_at=_aware(11, 5),
            contact_id=contact_a.id,
        )


# --------------------------------------------------------------------------
# Availability / overlap semantics (brief §7/§21.8,9)
# --------------------------------------------------------------------------


def test_no_events_means_available(two_tenants) -> None:
    context_a, _ = two_tenants
    calendar = create_calendar(context_a, name="Empty Calendar", timezone="UTC")
    result = check_availability(context_a, calendar.id, _aware(10, 6), _aware(11, 6))
    assert result.available is True


def test_overlapping_event_makes_it_unavailable(two_tenants) -> None:
    context_a, _ = two_tenants
    calendar = create_calendar(context_a, name="Busy Calendar", timezone="UTC")
    create_event(
        context_a,
        calendar_id=calendar.id,
        title="Existing",
        start_at=_aware(10, 7),
        end_at=_aware(11, 7),
    )
    result = check_availability(
        context_a,
        calendar.id,
        _aware(10, 7, minute=30),
        _aware(11, 7, minute=30),
    )
    assert result.available is False
    assert result.conflict_start_at is not None


def test_adjacent_events_do_not_conflict(two_tenants) -> None:
    context_a, _ = two_tenants
    calendar = create_calendar(context_a, name="Adjacency Calendar", timezone="UTC")
    create_event(
        context_a,
        calendar_id=calendar.id,
        title="First",
        start_at=_aware(10, 8),
        end_at=_aware(11, 8),
    )
    # Second appointment starts exactly when the first ends.
    second = create_event(
        context_a,
        calendar_id=calendar.id,
        title="Second",
        start_at=_aware(11, 8),
        end_at=_aware(12, 8),
    )
    assert second.status == "scheduled"
    result = check_availability(context_a, calendar.id, _aware(11, 8), _aware(12, 8))
    assert result.available is False  # occupied by "Second" itself
    # But the gap immediately preceding "First" is free.
    result = check_availability(context_a, calendar.id, _aware(9, 8), _aware(10, 8))
    assert result.available is True


def test_overlapping_appointment_creation_is_rejected(two_tenants) -> None:
    context_a, _ = two_tenants
    calendar = create_calendar(context_a, name="Conflict Calendar", timezone="UTC")
    create_event(
        context_a,
        calendar_id=calendar.id,
        title="First",
        start_at=_aware(10, 9),
        end_at=_aware(11, 9),
    )
    with pytest.raises(CalendarEventConflictError):
        create_event(
            context_a,
            calendar_id=calendar.id,
            title="Overlaps",
            start_at=_aware(10, 9, minute=30),
            end_at=_aware(11, 9, minute=30),
        )


def test_cancelled_event_does_not_block_availability(two_tenants) -> None:
    context_a, _ = two_tenants
    calendar = create_calendar(context_a, name="Cancellable Calendar", timezone="UTC")
    event = create_event(
        context_a,
        calendar_id=calendar.id,
        title="Will Cancel",
        start_at=_aware(10, 10),
        end_at=_aware(11, 10),
    )
    cancelled = cancel_event(context_a, event.id)
    assert cancelled.status == "cancelled"
    result = check_availability(context_a, calendar.id, _aware(10, 10), _aware(11, 10))
    assert result.available is True


def test_cancellation_is_idempotent(two_tenants) -> None:
    context_a, _ = two_tenants
    calendar = create_calendar(context_a, name="Idempotent Calendar", timezone="UTC")
    event = create_event(
        context_a,
        calendar_id=calendar.id,
        title="Once",
        start_at=_aware(10, 11),
        end_at=_aware(11, 11),
    )
    first = cancel_event(context_a, event.id)
    second = cancel_event(context_a, event.id)
    assert first.status == second.status == "cancelled"


def test_a_different_calendar_is_never_affected_by_another_calendars_events(two_tenants) -> None:
    context_a, _ = two_tenants
    calendar_1 = create_calendar(context_a, name="Calendar 1", timezone="UTC")
    calendar_2 = create_calendar(context_a, name="Calendar 2", timezone="UTC")
    create_event(
        context_a,
        calendar_id=calendar_1.id,
        title="Busy on 1",
        start_at=_aware(10, 12),
        end_at=_aware(11, 12),
    )
    result = check_availability(context_a, calendar_2.id, _aware(10, 12), _aware(11, 12))
    assert result.available is True


def test_availability_never_crosses_tenants(two_tenants) -> None:
    context_a, context_b = two_tenants
    calendar_a = create_calendar(context_a, name="Tenant A Calendar", timezone="UTC")
    with pytest.raises(CalendarNotFoundError):
        check_availability(context_b, calendar_a.id, _aware(10, 13), _aware(11, 13))


def test_list_events_filters_by_range(two_tenants) -> None:
    """Phase 2.15: `list_events()`'s first caller (`GET /v1/calendar-events`,
    the frontend's agenda view)."""
    context_a, _ = two_tenants
    calendar = create_calendar(context_a, name="Agenda Calendar", timezone="UTC")
    inside = create_event(
        context_a,
        calendar_id=calendar.id,
        title="Inside",
        start_at=_aware(10, 14),
        end_at=_aware(11, 14),
    )
    outside = create_event(
        context_a,
        calendar_id=calendar.id,
        title="Outside",
        start_at=_aware(10, 20),
        end_at=_aware(11, 20),
    )
    results = list_events(context_a, start_at=_aware(9, 14), end_at=_aware(12, 14))
    ids = {event.id for event in results}
    assert inside.id in ids
    assert outside.id not in ids


def test_list_events_can_filter_by_calendar(two_tenants) -> None:
    context_a, _ = two_tenants
    calendar_1 = create_calendar(context_a, name="Agenda Calendar 1", timezone="UTC")
    calendar_2 = create_calendar(context_a, name="Agenda Calendar 2", timezone="UTC")
    event_1 = create_event(
        context_a,
        calendar_id=calendar_1.id,
        title="On 1",
        start_at=_aware(10, 15),
        end_at=_aware(11, 15),
    )
    create_event(
        context_a,
        calendar_id=calendar_2.id,
        title="On 2",
        start_at=_aware(10, 15),
        end_at=_aware(11, 15),
    )
    results = list_events(
        context_a, start_at=_aware(9, 15), end_at=_aware(12, 15), calendar_id=calendar_1.id
    )
    assert {event.id for event in results} == {event_1.id}


def test_list_events_never_crosses_tenants(two_tenants) -> None:
    context_a, context_b = two_tenants
    calendar_a = create_calendar(context_a, name="Tenant A Agenda", timezone="UTC")
    create_event(
        context_a,
        calendar_id=calendar_a.id,
        title="A's event",
        start_at=_aware(10, 16),
        end_at=_aware(11, 16),
    )
    results_b = list_events(context_b, start_at=_aware(0, 16), end_at=_aware(23, 16))
    assert results_b == []
    with pytest.raises(CalendarNotFoundError):
        list_events(
            context_b, start_at=_aware(0, 16), end_at=_aware(23, 16), calendar_id=calendar_a.id
        )


# --------------------------------------------------------------------------
# Call <-> Contact association (brief §4/§16/§21.4)
# --------------------------------------------------------------------------


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


def test_associate_call_sets_contact_id(two_tenants) -> None:
    context_a, _ = two_tenants
    call = _make_call(context_a)
    contact = create_contact(context_a, name="Ada", phone_e164=_phone())

    updated = associate_call(context_a, call.id, contact.id)
    assert updated.contact_id == contact.id

    fetched = get_call_session(context_a, call.id)
    assert fetched.contact_id == contact.id


def test_associate_call_with_cross_tenant_contact_is_rejected(two_tenants) -> None:
    """Brief §4: no cross-tenant association -- a `contact_id` belonging to
    a different tenant is invisible under RLS, so this is the identical
    `ContactNotFoundError` a nonexistent id would raise."""
    context_a, context_b = two_tenants
    call = _make_call(context_a)
    contact_b = create_contact(context_b, name="Bob", phone_e164=_phone())

    with pytest.raises(ContactNotFoundError):
        associate_call(context_a, call.id, contact_b.id)

    fetched = get_call_session(context_a, call.id)
    assert fetched.contact_id is None


def test_associate_call_with_cross_tenant_call_is_rejected(two_tenants) -> None:
    context_a, context_b = two_tenants
    call_a = _make_call(context_a)
    contact_b = create_contact(context_b, name="Bob", phone_e164=_phone())

    with pytest.raises(CallSessionNotFoundError):
        associate_call(context_b, call_a.id, contact_b.id)


def test_call_lifecycle_is_unaffected_by_association(two_tenants) -> None:
    """Brief §4: the existing CallSession lifecycle remains unchanged."""
    from voiceagent.calls.service import transition_call_session

    context_a, _ = two_tenants
    call = _make_call(context_a)
    contact = create_contact(context_a, name="Ada", phone_e164=_phone())
    associate_call(context_a, call.id, contact.id)

    transitioned = transition_call_session(context_a, call.id, to_status="answered")
    assert transitioned.contact_id == contact.id
    assert transitioned.status == "answered"


# --------------------------------------------------------------------------
# Tool Gateway -> service -> DB under real tenant authorization (brief §21.10,11)
# --------------------------------------------------------------------------

SERVICE_ACCOUNT_NAME = "voiceagent-runtime"


@pytest.fixture
def tenant_and_admin():
    tenant = create_tenant(f"phase26-tools-{uuid.uuid4().hex[:8]}")
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


def test_contact_lookup_tool_executes_through_the_real_gateway(
    tenant_context, bootstrapped_service_account
) -> None:
    from voiceagent.runtime.db import DatabaseBoundary

    phone = _phone()
    create_contact(tenant_context, name="Ada", phone_e164=phone)
    call = _make_call(tenant_context)
    from voiceagent.agents.service import get_agent_version

    agent_version = get_agent_version(tenant_context, call.agent_version_id)
    # Give this published version the one tool under test (a fresh draft
    # would be required in production; this test only needs its `config`
    # shape, which `ToolGateway` reads directly).
    object.__setattr__(
        agent_version,
        "config",
        {**agent_version.config, "tools": [{"key": "contact.lookup_by_phone", "config": {}}]},
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
                call_id="c1", name="contact.lookup_by_phone", arguments={"phone_e164": phone}
            ),
        )
    finally:
        db.close()

    assert result.error_code is None
    assert result.value["found"] is True
    assert result.value["contact"]["phone_e164"] == phone


def test_calendar_create_appointment_tool_executes_through_the_real_gateway(
    tenant_context, bootstrapped_service_account
) -> None:
    from voiceagent.runtime.db import DatabaseBoundary

    calendar = create_calendar(tenant_context, name="Front Desk", timezone="UTC")
    call = _make_call(tenant_context)
    from voiceagent.agents.service import get_agent_version

    agent_version = get_agent_version(tenant_context, call.agent_version_id)
    object.__setattr__(
        agent_version,
        "config",
        {**agent_version.config, "tools": [{"key": "calendar.create_appointment", "config": {}}]},
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
                name="calendar.create_appointment",
                arguments={
                    "calendar_id": str(calendar.id),
                    "title": "Checkup",
                    "start_at": "2026-11-20T10:00:00+00:00",
                    "end_at": "2026-11-20T11:00:00+00:00",
                },
            ),
        )
    finally:
        db.close()

    assert result.error_code is None
    assert result.value["appointment"]["status"] == "scheduled"


def test_calendar_tool_not_in_allowlist_is_denied(
    tenant_context, bootstrapped_service_account
) -> None:
    """The AgentVersion allowlist still governs Phase 2.6 tools identically
    to Phase 2.4's -- being granted the RBAC permission is not enough."""
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
                call_id="c1", name="contact.lookup_by_phone", arguments={"phone_e164": _phone()}
            ),
        )
    finally:
        db.close()

    assert result.error_code == "tool_not_allowed"
