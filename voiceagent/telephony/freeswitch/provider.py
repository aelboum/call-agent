"""`FreeSwitchTelephonyProvider` -- the only `TelephonyProvider` implementation
(ADR-0002 point 1), over an injected `EslConnection` (ADR-0002 amendment
point 2). Every FreeSWITCH-specific concept -- ESL command syntax, event
field names, hangup-cause strings -- is translated at this module's boundary
and never crosses it: a caller sees only `voiceagent.telephony.contracts`
types.

The ESL command strings below (`uuid_answer`, `uuid_kill`, `uuid_bridge`,
`uuid_hold`, `uuid_send_dtmf`, `uuid_record`, `originate`) and the
`Event-Name`/`Hangup-Cause` values in the two maps are FreeSWITCH's
documented `mod_commands`/event vocabulary, carried forward from Phase 0
report §10.3-§10.5's own research pass -- **not independently re-verified
against a live FreeSWITCH instance in this phase** (brief section 29: no live
instance is required by the default CI suite). `docs/PHASE-2.2-STATUS.md`
records this explicitly as unverified-against-a-real-server, the same
posture Phase 0 report §10.4 already took for `mod_audio_stream`'s wire
protocol before its own conformance test existed.

**Phase 2.13 hardening: every command is bounded** (brief §10 "every
external provider operation on the live call path must have an explicit
timeout"). `EslConnection.send()` (`voiceagent.telephony.freeswitch.esl`) is
an injected protocol with no implementation yet in this repository (that
module's own docstring: establishing the real TCP transport is explicitly
out of scope) -- nothing before this phase bounded how long
`_command()` would wait for a response, so a wedged or slow-to-answer
control connection would hang whatever call operation was awaiting it (and,
transitively, that call's own teardown) indefinitely. `command_timeout_seconds`
normalizes a timeout into the existing `TransportError` taxonomy, the same
one a dropped/refused connection already raises, so callers do not need a
second error type to handle a slow command differently from a failed one.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator

from voiceagent.metrics import record_provider_operation
from voiceagent.telephony.contracts import (
    CallDirection,
    CallEvent,
    CallEventType,
    CallRef,
    HangupCause,
    OriginateRequest,
    TransportError,
)
from voiceagent.telephony.freeswitch.esl import EslConnection, EslEvent

__all__ = ["FreeSwitchTelephonyProvider"]

#: FreeSWITCH's `Event-Name` values, mapped onto the normalized taxonomy
#: `voiceagent.telephony.contracts.CallEventType` already defines. Any event
#: name not in this map is dropped by `events()` rather than raised -- an
#: unmapped event is not an error, it is simply not one this product's call
#: lifecycle (Phase 0 report §10.5) reacts to.
_EVENT_TYPE_MAP: dict[str, CallEventType] = {
    "CHANNEL_PARK": CallEventType.OFFERED,
    "CHANNEL_PROGRESS": CallEventType.RINGING,
    "CHANNEL_ANSWER": CallEventType.ANSWERED,
    "CHANNEL_BRIDGE": CallEventType.BRIDGED,
    "CHANNEL_HOLD": CallEventType.HELD,
    "CHANNEL_UNHOLD": CallEventType.RESUMED,
    "DTMF": CallEventType.DTMF,
    "RECORD_START": CallEventType.RECORDING_STARTED,
    "RECORD_STOP": CallEventType.RECORDING_STOPPED,
    "CHANNEL_HANGUP_COMPLETE": CallEventType.HUNGUP,
}

_HANGUP_CAUSE_MAP: dict[str, HangupCause] = {
    "NORMAL_CLEARING": HangupCause.NORMAL,
    "USER_BUSY": HangupCause.BUSY,
    "NO_ANSWER": HangupCause.NO_ANSWER,
    "CALL_REJECTED": HangupCause.REJECTED,
    "ORIGINATOR_CANCEL": HangupCause.CANCELED,
    "NETWORK_OUT_OF_ORDER": HangupCause.NETWORK_FAILURE,
    "RECOVERY_ON_TIMER_EXPIRE": HangupCause.TIMEOUT,
}


#: Phase 2.16 security audit: `originate()`/`send_dtmf()` build ESL command
#: strings by raw f-string interpolation, with no upstream validation of
#: `OriginateRequest.to_number`/`from_number` or `send_dtmf()`'s `digits` at
#: the contract level (`voiceagent.telephony.contracts.OriginateRequest` is a
#: plain, unvalidated dataclass). Today, `originate()`/`send_dtmf()` have no
#: reachable caller anywhere in this product (`transfer()`'s own
#: `destination` is the only phone-number-shaped value the Tool Gateway ever
#: lets a model influence, and it is already validated pre-handler by
#: `voiceagent.tools.handlers.TransferInput`) -- but this module must not
#: rely on that staying true. These two patterns are the same defense
#: `TransferInput` already applies, enforced a second time, here, at the one
#: place these values actually reach an ESL command string -- so a future
#: caller of either method is protected regardless of whether it remembers
#: to validate first.
_E164_PATTERN = re.compile(r"^\+[1-9]\d{1,14}$")
#: FreeSWITCH's own accepted DTMF alphabet: digits, `*`, `#`, and `w`/`W` for
#: an inter-digit pause -- never a character that could appear in ESL syntax.
_DTMF_PATTERN = re.compile(r"^[0-9*#wW]{1,32}$")


def _require_e164(value: str, *, field: str) -> None:
    if not _E164_PATTERN.match(value):
        raise TransportError(f"{field} is not a valid E.164 phone number")


def _require_dtmf_digits(value: str) -> None:
    if not _DTMF_PATTERN.match(value):
        raise TransportError("digits is not a valid DTMF digit string")


def _normalize_hangup_cause(raw: str | None) -> HangupCause:
    if raw is None:
        return HangupCause.UNKNOWN
    return _HANGUP_CAUSE_MAP.get(raw, HangupCause.UNKNOWN)


def _denormalize_hangup_cause(cause: HangupCause) -> str:
    for raw, normalized in _HANGUP_CAUSE_MAP.items():
        if normalized is cause:
            return raw
    return "NORMAL_CLEARING"


def _normalize_event(raw: EslEvent) -> CallEvent | None:
    event_type = _EVENT_TYPE_MAP.get(raw.get("Event-Name", ""))
    if event_type is None:
        return None
    call_ref: CallRef = raw.get("Unique-ID", "")
    direction = (
        CallDirection.OUTBOUND if raw.get("Call-Direction") == "outbound" else CallDirection.INBOUND
    )
    return CallEvent(
        type=event_type,
        call_ref=call_ref,
        direction=direction,
        from_number=raw.get("Caller-Caller-ID-Number"),
        to_number=raw.get("Caller-Destination-Number"),
        hangup_cause=(
            _normalize_hangup_cause(raw.get("Hangup-Cause"))
            if event_type is CallEventType.HUNGUP
            else None
        ),
        digit=raw.get("DTMF-Digit") if event_type is CallEventType.DTMF else None,
    )


class FreeSwitchTelephonyProvider:
    """Call control over `mod_event_socket`, inbound mode (ADR-0002 point 4).
    Issues ESL command strings via the injected `EslConnection` and
    normalizes its raw event stream into `CallEvent` -- the only
    FreeSWITCH-specific code path any command or event in this product ever
    passes through."""

    def __init__(self, esl: EslConnection, *, command_timeout_seconds: float = 10.0) -> None:
        self._esl = esl
        self._command_timeout_seconds = command_timeout_seconds

    async def _command(self, command: str, *, operation: str) -> str:
        """`operation` is one of this class's own ten bounded ESL verb names
        below (Phase 2.14, brief section 6) -- never derived from `command`
        itself, which carries call-specific values (a `call_ref`, a phone
        number) that must never become a metric label."""
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._esl.send(command), timeout=self._command_timeout_seconds
            )
        except TimeoutError as exc:
            record_provider_operation("telephony", operation, "timeout", time.monotonic() - started)
            raise TransportError(
                f"ESL command timed out after {self._command_timeout_seconds}s: {command!r}"
            ) from exc
        if response.startswith("-ERR"):
            record_provider_operation("telephony", operation, "failure", time.monotonic() - started)
            raise TransportError(f"ESL command failed: {command!r} -> {response!r}")
        record_provider_operation("telephony", operation, "success", time.monotonic() - started)
        return response

    async def originate(self, request: OriginateRequest) -> CallRef:
        _require_e164(request.from_number, field="from_number")
        _require_e164(request.to_number, field="to_number")
        response = await self._command(
            f"bgapi originate "
            f"{{origination_caller_id_number={request.from_number}}}"
            f"sofia/gateway/default/{request.to_number}",
            operation="originate",
        )
        return response.strip()

    async def answer(self, call_ref: CallRef) -> None:
        await self._command(f"uuid_answer {call_ref}", operation="answer")

    async def hangup(self, call_ref: CallRef, cause: HangupCause = HangupCause.NORMAL) -> None:
        await self._command(
            f"uuid_kill {call_ref} {_denormalize_hangup_cause(cause)}", operation="hangup"
        )

    async def bridge(self, call_ref: CallRef, other_call_ref: CallRef) -> None:
        await self._command(f"uuid_bridge {call_ref} {other_call_ref}", operation="bridge")

    async def transfer(self, call_ref: CallRef, destination: str) -> CallRef:
        response = await self._command(
            f"bgapi originate sofia/gateway/default/{destination}", operation="transfer"
        )
        return response.strip()

    async def hold(self, call_ref: CallRef) -> None:
        await self._command(f"uuid_hold {call_ref}", operation="hold")

    async def unhold(self, call_ref: CallRef) -> None:
        await self._command(f"uuid_hold off {call_ref}", operation="unhold")

    async def send_dtmf(self, call_ref: CallRef, digits: str) -> None:
        _require_dtmf_digits(digits)
        await self._command(f"uuid_send_dtmf {call_ref} {digits}", operation="send_dtmf")

    async def start_recording(self, call_ref: CallRef) -> None:
        await self._command(f"uuid_record {call_ref} start /dev/null", operation="start_recording")

    async def stop_recording(self, call_ref: CallRef) -> None:
        await self._command(f"uuid_record {call_ref} stop /dev/null", operation="stop_recording")

    async def events(self) -> AsyncIterator[CallEvent]:
        async for raw in self._esl.events():
            normalized = _normalize_event(raw)
            if normalized is not None:
                yield normalized
