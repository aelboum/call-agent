"""Phase 2.16 security audit: `originate()`/`send_dtmf()` validate their
phone-number/DTMF-shaped arguments before building an ESL command string --
defense in depth against command injection, applied even though nothing in
this codebase currently calls either method with attacker-influenced input
(see `voiceagent/telephony/freeswitch/provider.py`'s own module comment for
why this is still worth enforcing here, not just upstream)."""

from __future__ import annotations

import asyncio

import pytest

from voiceagent.telephony.contracts import OriginateRequest, TransportError
from voiceagent.telephony.freeswitch.fakes import FakeEslConnection
from voiceagent.telephony.freeswitch.provider import FreeSwitchTelephonyProvider


def test_originate_accepts_valid_e164_numbers() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl, uuid_factory=lambda: "fixed-uuid")
    call_ref = asyncio.run(
        provider.originate(OriginateRequest(to_number="+15551234567", from_number="+15557654321"))
    )
    assert call_ref == "fixed-uuid"
    assert esl.commands == [
        "bgapi originate {origination_uuid=fixed-uuid,"
        "origination_caller_id_number=+15557654321}sofia/gateway/default/+15551234567"
    ]


@pytest.mark.parametrize(
    "to_number",
    [
        "+1555123456; hangup",  # command-separator-shaped
        "+1555}api reload{",  # ESL brace-shaped
        "not-a-number",
        "",
        "+0123456789",  # leading zero after + is not valid E.164
    ],
)
def test_originate_rejects_a_malformed_to_number_before_touching_the_esl_connection(
    to_number: str,
) -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    with pytest.raises(TransportError):
        asyncio.run(
            provider.originate(OriginateRequest(to_number=to_number, from_number="+15551234567"))
        )
    # The malformed value never reached the ESL connection at all -- the
    # rejection happens before `_command()` is ever called.
    assert esl.commands == []


def test_originate_rejects_a_malformed_from_number() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    with pytest.raises(TransportError):
        asyncio.run(
            provider.originate(
                OriginateRequest(to_number="+15551234567", from_number="+1555} bgapi reload {")
            )
        )
    assert esl.commands == []


def test_transfer_rejects_a_malformed_destination_before_touching_the_esl_connection() -> None:
    """Phase 2.21: `transfer()` gained the identical E.164 defense-in-depth
    `originate()` already had -- it builds an ESL command string from
    `destination` too."""
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    with pytest.raises(TransportError):
        asyncio.run(provider.transfer("call-1", "+1555} bgapi reload {"))
    assert esl.commands == []


def test_send_dtmf_accepts_valid_digit_strings() -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    asyncio.run(provider.send_dtmf("call-1", "123*#w4"))
    assert esl.commands == ["uuid_send_dtmf call-1 123*#w4"]


@pytest.mark.parametrize(
    "digits",
    [
        "1;api reload",
        "1 2",  # a space could terminate the ESL command early
        "",
        "1" * 33,  # exceeds the bounded length even though every char is valid
    ],
)
def test_send_dtmf_rejects_a_malformed_digit_string(digits: str) -> None:
    esl = FakeEslConnection()
    provider = FreeSwitchTelephonyProvider(esl)
    with pytest.raises(TransportError):
        asyncio.run(provider.send_dtmf("call-1", digits))
    assert esl.commands == []
