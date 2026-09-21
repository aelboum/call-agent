"""Phase 2.4's four built-in call-control tool handlers, exercised directly
against `FakeTelephonyProvider` -- the isolation properties (no FreeSWITCH
import, no DB import) are covered separately by
`tests/architecture/test_tool_gateway_isolation.py`.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from pydantic import ValidationError

from voiceagent.telephony.contracts import HangupCause
from voiceagent.telephony.fakes import FakeTelephonyProvider, UnknownCallError
from voiceagent.tools.definitions import ToolExecutionContext
from voiceagent.tools.errors import ToolExecutionError
from voiceagent.tools.handlers import (
    HangupInput,
    HoldInput,
    ResumeInput,
    TransferInput,
    _hangup,
    _hold,
    _resume,
    _transfer,
)


def _ctx(telephony, call_ref: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=uuid.uuid4(),
        call_session_id=uuid.uuid4(),
        agent_version_id=uuid.uuid4(),
        tool_call_id="call-1",
        correlation_id="call-1",
        call_ref=call_ref,
        telephony=telephony,
    )


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
