"""Phase 2.4's four built-in call-control tool handlers, exercised directly
against `FakeTelephonyProvider` -- the isolation properties (no FreeSWITCH
import, no DB import) are covered separately by
`tests/architecture/test_tool_gateway_isolation.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from voiceagent.calendars import service as calendar_service
from voiceagent.calendars.errors import (
    CalendarEventConflictError,
    CalendarEventNotFoundError,
    CalendarNotFoundError,
)
from voiceagent.calendars.models import CalendarEvent
from voiceagent.contacts import service as contact_service
from voiceagent.contacts.errors import ContactNotFoundError
from voiceagent.contacts.models import Contact
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.telephony.contracts import HangupCause
from voiceagent.telephony.fakes import FakeTelephonyProvider, UnknownCallError
from voiceagent.tenancy import TenantContext
from voiceagent.tools.definitions import ToolExecutionContext
from voiceagent.tools.errors import ToolExecutionError
from voiceagent.tools.handlers import (
    CancelAppointmentInput,
    CheckAvailabilityInput,
    CreateAppointmentInput,
    HangupInput,
    HoldInput,
    LookupContactByPhoneInput,
    ResumeInput,
    TransferInput,
    _cancel_appointment,
    _check_availability,
    _create_appointment,
    _hangup,
    _hold,
    _lookup_contact_by_phone,
    _resume,
    _transfer,
)


def _ctx(telephony, call_ref: str, **overrides) -> ToolExecutionContext:
    fields = {
        "tenant_id": uuid.uuid4(),
        "call_session_id": uuid.uuid4(),
        "agent_version_id": uuid.uuid4(),
        "tool_call_id": "call-1",
        "correlation_id": "call-1",
        "call_ref": call_ref,
        "telephony": telephony,
    }
    fields.update(overrides)
    return ToolExecutionContext(**fields)


@pytest.fixture
def db():
    boundary = DatabaseBoundary(max_workers=2)
    yield boundary
    boundary.close()


def _db_ctx(db, telephony=None) -> ToolExecutionContext:
    telephony = telephony or FakeTelephonyProvider()
    tenant_context = TenantContext(
        tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), membership_id=uuid.uuid4()
    )
    return _ctx(telephony, "ref", tenant_context=tenant_context, db=db)


def test_hangup_calls_telephony_hangup_on_this_calls_ref() -> None:
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+15551234567", to_number="+15557654321")
    result = asyncio.run(_hangup(_ctx(telephony, call_ref), HangupInput()))
    assert result == {"hung_up": True}
    assert ("hangup", call_ref, HangupCause.NORMAL.value) in telephony.commands
    assert call_ref not in telephony.live_calls


def test_hangup_normalizes_a_telephony_failure() -> None:
    telephony = FakeTelephonyProvider()
    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(_hangup(_ctx(telephony, "never-offered"), HangupInput()))
    assert exc_info.value.code == "telephony_error"
    assert exc_info.value.retryable is False


def test_hold_and_resume_round_trip() -> None:
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")

    async def scenario() -> None:
        await _hold(_ctx(telephony, call_ref), HoldInput())
        assert call_ref in telephony.held
        await _resume(_ctx(telephony, call_ref), ResumeInput())
        assert call_ref not in telephony.held

    asyncio.run(scenario())


def test_transfer_passes_the_validated_destination_through() -> None:
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")
    tool_input = TransferInput(destination_e164="+15559998888")
    result = asyncio.run(_transfer(_ctx(telephony, call_ref), tool_input))
    assert result == {"transferred": True, "destination_e164": "+15559998888"}
    assert ("transfer", call_ref, "+15559998888") in telephony.commands


@pytest.mark.parametrize(
    "destination",
    [
        "not-a-number",
        "15559998888",  # missing leading +
        "+0123456789",  # leading zero after +
        "+1; DROP TABLE calls;",  # injection-shaped
        "",
    ],
)
def test_transfer_input_rejects_a_malformed_destination(destination: str) -> None:
    with pytest.raises(ValidationError):
        TransferInput(destination_e164=destination)


def test_transfer_input_rejects_unexpected_extra_fields() -> None:
    """The model's `extra='forbid'` is what stops an LLM from smuggling an
    identity-shaped field (e.g. `tenant_id`) through tool arguments (Phase
    2.4 brief section 7)."""
    with pytest.raises(ValidationError):
        TransferInput.model_validate(
            {"destination_e164": "+15559998888", "tenant_id": str(uuid.uuid4())}
        )


def test_repeated_hangup_on_an_already_ended_call_fails_closed_not_silently() -> None:
    """A *second*, distinct tool call (different `call_id`, not a replay of
    the same one -- idempotency replay is the gateway's job, tested in
    `tests/tools/test_gateway.py`) that reaches the handler after the call
    already ended surfaces as a normal, normalized failure rather than a
    raw `UnknownCallError`."""
    telephony = FakeTelephonyProvider()
    call_ref = telephony.offer_inbound(from_number="+1", to_number="+2")

    async def scenario() -> None:
        await telephony.hangup(call_ref)
        with pytest.raises(ToolExecutionError):
            await _hangup(_ctx(telephony, call_ref), HangupInput())
        # Confirms the fake itself actually raises what the handler must
        # catch -- otherwise this test would pass for the wrong reason.
        with pytest.raises(UnknownCallError):
            await telephony.hold(call_ref)

    asyncio.run(scenario())


# -- Phase 2.6: contact.lookup_by_phone --------------------------------------


def test_lookup_contact_by_phone_found(db, monkeypatch) -> None:
    contact = Contact(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), name="Ada", phone_e164="+15551234567", email=None
    )
    monkeypatch.setattr(contact_service, "lookup_contact_by_phone", lambda ctx, phone: contact)
    ctx = _db_ctx(db)
    result = asyncio.run(
        _lookup_contact_by_phone(ctx, LookupContactByPhoneInput(phone_e164="+15551234567"))
    )
    assert result["found"] is True
    contact_out = result["contact"]
    assert isinstance(contact_out, dict)
    assert contact_out["name"] == "Ada"
    assert contact_out["phone_e164"] == "+15551234567"


def test_lookup_contact_by_phone_not_found_is_explicit(db, monkeypatch) -> None:
    monkeypatch.setattr(contact_service, "lookup_contact_by_phone", lambda ctx, phone: None)
    ctx = _db_ctx(db)
    result = asyncio.run(
        _lookup_contact_by_phone(ctx, LookupContactByPhoneInput(phone_e164="+15551234567"))
    )
    assert result == {"found": False, "contact": None}


def test_lookup_contact_by_phone_rejects_unexpected_extra_fields() -> None:
    with pytest.raises(ValidationError):
        LookupContactByPhoneInput.model_validate(
            {"phone_e164": "+15551234567", "tenant_id": str(uuid.uuid4())}
        )


# -- Phase 2.6: calendar.check_availability ----------------------------------


def test_check_availability_when_available(db, monkeypatch) -> None:
    result = calendar_service.AvailabilityResult(available=True)
    monkeypatch.setattr(calendar_service, "check_availability", lambda *a, **kw: result)
    ctx = _db_ctx(db)
    tool_input = CheckAvailabilityInput(
        calendar_id=uuid.uuid4(),
        start_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 10, 1, 11, tzinfo=UTC),
    )
    output = asyncio.run(_check_availability(ctx, tool_input))
    assert output == {"available": True, "conflict_start_at": None, "conflict_end_at": None}


def test_check_availability_when_conflicting(db, monkeypatch) -> None:
    conflict_start = datetime(2026, 10, 1, 10, tzinfo=UTC)
    conflict_end = datetime(2026, 10, 1, 11, tzinfo=UTC)
    result = calendar_service.AvailabilityResult(
        available=False, conflict_start_at=conflict_start, conflict_end_at=conflict_end
    )
    monkeypatch.setattr(calendar_service, "check_availability", lambda *a, **kw: result)
    ctx = _db_ctx(db)
    tool_input = CheckAvailabilityInput(
        calendar_id=uuid.uuid4(), start_at=conflict_start, end_at=conflict_end
    )
    output = asyncio.run(_check_availability(ctx, tool_input))
    assert output["available"] is False
    assert output["conflict_start_at"] == conflict_start


def test_check_availability_normalizes_calendar_not_found(db, monkeypatch) -> None:
    def _raise(*a, **kw):
        raise CalendarNotFoundError(uuid.uuid4())

    monkeypatch.setattr(calendar_service, "check_availability", _raise)
    ctx = _db_ctx(db)
    tool_input = CheckAvailabilityInput(
        calendar_id=uuid.uuid4(),
        start_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 10, 1, 11, tzinfo=UTC),
    )
    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(_check_availability(ctx, tool_input))
    assert exc_info.value.code == "calendar_not_found"


def test_check_availability_input_rejects_naive_datetime() -> None:
    """`AwareDatetime` rejects a naive datetime at the model boundary --
    never silently interpreted as UTC (brief §6)."""
    with pytest.raises(ValidationError):
        CheckAvailabilityInput(
            calendar_id=uuid.uuid4(),
            start_at=datetime(2026, 10, 1, 10),
            end_at=datetime(2026, 10, 1, 11),
        )


# -- Phase 2.6: calendar.create_appointment ----------------------------------


def test_create_appointment_success(db, monkeypatch) -> None:
    event = CalendarEvent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        calendar_id=uuid.uuid4(),
        contact_id=None,
        title="Checkup",
        start_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 10, 1, 11, tzinfo=UTC),
        status="scheduled",
    )
    monkeypatch.setattr(calendar_service, "create_event", lambda *a, **kw: event)
    ctx = _db_ctx(db)
    tool_input = CreateAppointmentInput(
        calendar_id=uuid.uuid4(),
        title="Checkup",
        start_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 10, 1, 11, tzinfo=UTC),
    )
    output = asyncio.run(_create_appointment(ctx, tool_input))
    appointment_out = output["appointment"]
    assert isinstance(appointment_out, dict)
    assert appointment_out["status"] == "scheduled"
    assert appointment_out["contact_id"] is None


@pytest.mark.parametrize(
    "raised, expected_code",
    [
        (CalendarNotFoundError(uuid.uuid4()), "calendar_not_found"),
        (ContactNotFoundError(uuid.uuid4()), "contact_not_found"),
        (CalendarEventConflictError(), "conflict"),
    ],
)
def test_create_appointment_normalizes_domain_errors(
    db, monkeypatch, raised, expected_code
) -> None:
    def _raise(*a, **kw):
        raise raised

    monkeypatch.setattr(calendar_service, "create_event", _raise)
    ctx = _db_ctx(db)
    tool_input = CreateAppointmentInput(
        calendar_id=uuid.uuid4(),
        title="Checkup",
        start_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 10, 1, 11, tzinfo=UTC),
    )
    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(_create_appointment(ctx, tool_input))
    assert exc_info.value.code == expected_code


def test_create_appointment_rejects_unexpected_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CreateAppointmentInput.model_validate(
            {
                "calendar_id": str(uuid.uuid4()),
                "title": "Checkup",
                "start_at": "2026-10-01T10:00:00+00:00",
                "end_at": "2026-10-01T11:00:00+00:00",
                "tenant_id": str(uuid.uuid4()),
            }
        )


# -- Phase 2.6: calendar.cancel_appointment ----------------------------------


def test_cancel_appointment_success(db, monkeypatch) -> None:
    event = CalendarEvent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        calendar_id=uuid.uuid4(),
        contact_id=None,
        title="Checkup",
        start_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
        end_at=datetime(2026, 10, 1, 11, tzinfo=UTC),
        status="cancelled",
    )
    monkeypatch.setattr(calendar_service, "cancel_event", lambda *a, **kw: event)
    ctx = _db_ctx(db)
    output = asyncio.run(_cancel_appointment(ctx, CancelAppointmentInput(event_id=event.id)))
    assert output == {"event_id": str(event.id), "status": "cancelled"}


def test_cancel_appointment_normalizes_not_found(db, monkeypatch) -> None:
    def _raise(*a, **kw):
        raise CalendarEventNotFoundError(uuid.uuid4())

    monkeypatch.setattr(calendar_service, "cancel_event", _raise)
    ctx = _db_ctx(db)
    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(_cancel_appointment(ctx, CancelAppointmentInput(event_id=uuid.uuid4())))
    assert exc_info.value.code == "event_not_found"
