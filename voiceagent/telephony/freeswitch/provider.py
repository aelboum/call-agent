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
import contextlib
import json
import re
import time
import uuid as uuid_module
from collections.abc import AsyncIterator, Callable

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
from voiceagent.telephony.freeswitch.media_transport import mint_media_ticket

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

#: Phase 2.26: `mod_audio_stream`'s own event subclass (`mod_audio_stream.h`,
#: `EVENT_PLAY`) for "I decoded a `streamAudio` playback payload and wrote it
#: to this temp file" -- see `_handle_play_event()`'s own docstring for why
#: this module, not the module itself, is the one that actually plays it.
_PLAY_EVENT_SUBCLASS = "mod_audio_stream::play"
#: The `file` path `mod_audio_stream` itself generates (its own
#: `audio_streamer_glue.cpp`: `"%s%s%s_%d.tmp%s"`, built from
#: `SWITCH_GLOBAL_dirs.temp_dir`, the channel UUID, a per-file counter, and a
#: fixed extension) -- never influenced by this product or by anything a
#: caller/tenant controls. Validated anyway, defense in depth, the same
#: posture `_E164_PATTERN`/`_DTMF_PATTERN` already take for values that
#: reach an ESL command string: reject anything shaped differently (a
#: character that could break `uuid_broadcast`'s own space-delimited
#: argument parsing, e.g. a space or a shell/ESL metacharacter) rather than
#: interpolate it unchecked.
_PLAYBACK_FILE_PATTERN = re.compile(r"^/[A-Za-z0-9_./-]+$")


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

    def __init__(
        self,
        esl: EslConnection,
        *,
        command_timeout_seconds: float = 10.0,
        uuid_factory: Callable[[], str] = lambda: str(uuid_module.uuid4()),
        media_public_base_url: str | None = None,
        media_ticket_secret_provider: Callable[[], str] | None = None,
        media_ticket_ttl_seconds: float = 60.0,
    ) -> None:
        self._esl = esl
        self._command_timeout_seconds = command_timeout_seconds
        # Phase 2.21: the *product* mints the correlation id for a new call
        # leg, never FreeSWITCH (`docs/PHASE-0-ARCHITECTURE.md` §10.5's
        # documented design: "a product-generated `origination_uuid`,
        # stamped as a channel variable" -- see `originate()`/`transfer()`
        # below for why). Injectable so a test can assert an exact,
        # deterministic ESL command string.
        self._uuid_factory = uuid_factory
        # Phase 2.22: `start_media_stream()` mints and owns its own media
        # ticket -- moved here (rather than left as a caller-supplied
        # `media_url`, Phase 2.21's own original shape) once a real caller
        # (`voiceagent.runtime.orchestrator.CallOrchestrator`) needed one:
        # minting requires `voiceagent.telephony.freeswitch.media_transport
        # .mint_media_ticket()`, and the orchestrator must never import
        # anything under `voiceagent.telephony.freeswitch` directly (brief
        # section 18) -- exactly the boundary this move preserves. `None`
        # for both is valid construction (every Phase 2.21 test, and any
        # caller that never uses `start_media_stream()`); calling
        # `start_media_stream()` itself without them is a programming error,
        # raised explicitly rather than producing a malformed URL.
        self._media_public_base_url = (
            media_public_base_url.rstrip("/") if media_public_base_url is not None else None
        )
        self._media_ticket_secret_provider = media_ticket_secret_provider
        self._media_ticket_ttl_seconds = media_ticket_ttl_seconds

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
        """Phase 2.21 correction: `bgapi originate`'s own immediate reply is
        a Job-UUID (the background job that *attempts* the origination), not
        the resulting channel's UUID -- treating it as `CallRef` (the
        pre-Phase-2.21 behavior) was never verified against a live server
        and does not match documented ESL semantics. This method instead
        mints the `CallRef` itself and stamps it into the dial string as
        `origination_uuid`, which forces FreeSWITCH to use exactly that
        value as the new channel's own `Unique-ID` -- so every later event
        for this call (`ANSWER`, `HANGUP`, ...) already carries the same
        `call_ref` this method returns, with no reply-parsing and no
        dependency on `BACKGROUND_JOB` correlation at all."""
        _require_e164(request.from_number, field="from_number")
        _require_e164(request.to_number, field="to_number")
        call_ref = self._uuid_factory()
        await self._command(
            f"bgapi originate "
            f"{{origination_uuid={call_ref},"
            f"origination_caller_id_number={request.from_number}}}"
            f"sofia/gateway/default/{request.to_number}",
            operation="originate",
        )
        return call_ref

    async def answer(self, call_ref: CallRef) -> None:
        await self._command(f"api uuid_answer {call_ref}", operation="answer")

    async def hangup(self, call_ref: CallRef, cause: HangupCause = HangupCause.NORMAL) -> None:
        await self._command(
            f"api uuid_kill {call_ref} {_denormalize_hangup_cause(cause)}", operation="hangup"
        )

    async def bridge(self, call_ref: CallRef, other_call_ref: CallRef) -> None:
        await self._command(f"api uuid_bridge {call_ref} {other_call_ref}", operation="bridge")

    async def transfer(self, call_ref: CallRef, destination: str) -> CallRef:
        """Same correction as `originate()`: the new leg's `CallRef` is
        minted here, not parsed from FreeSWITCH's job-dispatch reply."""
        _require_e164(destination, field="destination")
        new_call_ref = self._uuid_factory()
        await self._command(
            f"bgapi originate {{origination_uuid={new_call_ref}}}"
            f"sofia/gateway/default/{destination}",
            operation="transfer",
        )
        return new_call_ref

    async def hold(self, call_ref: CallRef) -> None:
        await self._command(f"api uuid_hold {call_ref}", operation="hold")

    async def unhold(self, call_ref: CallRef) -> None:
        await self._command(f"api uuid_hold off {call_ref}", operation="unhold")

    async def send_dtmf(self, call_ref: CallRef, digits: str) -> None:
        _require_dtmf_digits(digits)
        await self._command(f"api uuid_send_dtmf {call_ref} {digits}", operation="send_dtmf")

    async def start_recording(self, call_ref: CallRef) -> None:
        await self._command(
            f"api uuid_record {call_ref} start /dev/null", operation="start_recording"
        )

    async def stop_recording(self, call_ref: CallRef) -> None:
        await self._command(
            f"api uuid_record {call_ref} stop /dev/null", operation="stop_recording"
        )

    async def start_media_stream(self, call_ref: CallRef) -> None:
        """Command FreeSWITCH's `mod_audio_stream` to open the product's own
        `wss://` media listener for this call leg (`uuid_audio_stream <uuid>
        start <url> mono 8k`, PSTN default per `voiceagent.telephony
        .contracts.AudioFormat`'s own default). Deliberately **not** part of
        `voiceagent.telephony.contracts.TelephonyProvider`: which command
        starts media (if any -- a different vendor's mechanism could differ
        completely) is FreeSWITCH-specific, not something the vendor-neutral
        contract should assume.

        Mints and owns its own short-lived, signed media ticket (Phase
        2.22) -- the caller (`voiceagent.runtime.orchestrator
        .CallOrchestrator`) supplies only `call_ref`, never a URL or a
        ticket, keeping every FreeSWITCH/`mod_audio_stream` concept
        (including the ticket's own transport) confined to this package."""
        if self._media_public_base_url is None or self._media_ticket_secret_provider is None:
            raise TransportError(
                "start_media_stream() requires media_public_base_url and "
                "media_ticket_secret_provider to be configured at construction time"
            )
        ticket = mint_media_ticket(
            call_ref,
            self._media_ticket_secret_provider(),
            ttl_seconds=self._media_ticket_ttl_seconds,
        )
        media_url = f"{self._media_public_base_url}/media/{ticket}"
        await self._command(
            f"api uuid_audio_stream {call_ref} start {media_url} mono 8k",
            operation="start_media_stream",
        )

    async def stop_media_stream(self, call_ref: CallRef) -> None:
        await self._command(f"api uuid_audio_stream {call_ref} stop", operation="stop_media_stream")

    async def _handle_play_event(self, raw: EslEvent) -> None:
        """Phase 2.26: `mod_audio_stream`'s own build at this product's
        pinned FreeSWITCH image commit (`ec2a781`, `amigniter/
        mod_audio_stream`) implements exactly half of "play this audio to
        the caller" -- it decodes a `streamAudio` payload, writes it to a
        temp file, and fires `mod_audio_stream::play` (this module's own
        `EVENT_PLAY`) naming that file. It never calls FreeSWITCH's own
        channel-audio-injection API itself (verified empirically: reading
        this exact pinned commit's own `audio_streamer_glue.cpp` top to
        bottom, there is no `switch_ivr_broadcast`/`switch_ivr_play_file`
        call anywhere in it) -- by this vendor's own design (its README:
        the commercial edition, not this pinned free one, adds "automatic
        playback"; the free edition's own event exists precisely so an ESL
        listener can finish the job itself). This is the one place in this
        product with a live ESL connection to react to that event, so it is
        the one place that closes the loop: `uuid_broadcast <call_ref>
        <file> aleg` is FreeSWITCH's own documented mechanism for playing a
        file to one leg of a live channel -- confirmed against this exact
        pinned image via a real SIP call, real RTP, and a real captured
        tone (docs/PHASE-2.26-REAL-SIP-TTS-PLAYBACK.md).

        Never raises: a malformed/absent body, an unparseable JSON payload,
        an unexpected `file` shape, or a failed/timed-out broadcast (e.g.
        the caller already hung up) all simply mean no audio is played for
        this one event -- never a reason to tear down the whole event
        stream every other call on this connection depends on."""
        call_ref = raw.get("Unique-ID", "")
        body = raw.get("__body__")
        if not call_ref or not body:
            return
        try:
            payload = json.loads(body)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        file_path = payload.get("file")
        if not isinstance(file_path, str) or not _PLAYBACK_FILE_PATTERN.match(file_path):
            return
        with contextlib.suppress(TransportError):
            await self._command(
                f"api uuid_broadcast {call_ref} {file_path} aleg", operation="broadcast_play"
            )

    async def events(self) -> AsyncIterator[CallEvent]:
        async for raw in self._esl.events():
            if (
                raw.get("Event-Name") == "CUSTOM"
                and raw.get("Event-Subclass") == _PLAY_EVENT_SUBCLASS
            ):
                await self._handle_play_event(raw)
                continue
            normalized = _normalize_event(raw)
            if normalized is not None:
                yield normalized
