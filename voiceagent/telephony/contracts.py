"""Telephony contracts (ADR-0002, 2026-09-21 amendment).

Two narrow, product-owned interfaces. FreeSWITCH is an implementation behind
them, not a dependency of the application:

* `TelephonyProvider` -- call control.
* `MediaProvider` -- media transport.

They are split because control and media have different lifetimes, failure
modes and scaling properties: a media stream can drop while the call stays up,
and media may terminate on a different process than the one holding the
control connection.

**These interfaces are deliberately not a vendor union.** They contain only
what this product's call lifecycle needs. Specifically excluded, and to stay
excluded: webhook signature verification, status-callback parsing, per-call
cost lookup, phone-number provisioning, answering-machine-detection
parameters, and anything else that exists only because some hosted CPaaS has
it. Growing toward the union of every vendor's vocabulary is the failure mode
rejected in Phase 0 report section 3.4.

Nothing here names FreeSWITCH, ESL, SIP, RTP or `mod_audio_stream`. A call is
identified by an opaque `CallRef`; a hangup reason is a normalized
`HangupCause`, never a carrier's own string. Phase 1 defines the contracts
and a fake; no real implementation exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

__all__ = [
    "AudioFormat",
    "CallDirection",
    "CallEvent",
    "CallEventType",
    "CallRef",
    "HangupCause",
    "MediaProvider",
    "MediaStream",
    "OriginateRequest",
    "StreamHealth",
    "TelephonyError",
    "TelephonyProvider",
    "TransportError",
    "UnsupportedFormatError",
]


class TelephonyError(Exception):
    """Base class for every error a telephony or media adapter raises.

    An adapter never lets a transport-specific exception escape: the runtime
    reacts to this taxonomy, not to a vendor's exception types.
    """


class TransportError(TelephonyError):
    """The control or media transport failed (dropped, refused, timed out)."""


class UnsupportedFormatError(TelephonyError):
    """The requested audio format cannot be provided for this call leg."""


#: An opaque handle for one call leg, issued by the `TelephonyProvider`.
#: Application code treats it as an identifier and never parses it -- what it
#: encodes is the adapter's business.
type CallRef = str


class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class HangupCause(StrEnum):
    """Normalized hangup reasons. An adapter maps its transport's own causes
    onto these; the domain never sees a carrier string."""

    NORMAL = "normal"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    REJECTED = "rejected"
    CANCELED = "canceled"
    NETWORK_FAILURE = "network_failure"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class CallEventType(StrEnum):
    """The normalized call-lifecycle events the product reacts to."""

    RINGING = "ringing"
    OFFERED = "offered"
    ANSWERED = "answered"
    DESTINATION_ANSWERED = "destination_answered"
    BRIDGED = "bridged"
    HELD = "held"
    RESUMED = "resumed"
    DTMF = "dtmf"
    RECORDING_STARTED = "recording_started"
    RECORDING_STOPPED = "recording_stopped"
    HUNGUP = "hungup"


@dataclass(frozen=True, slots=True)
class CallEvent:
    """One normalized lifecycle event.

    `OFFERED` is the event on which the product resolves the called number to
    a tenant and an agent version, server-side. `to_number`/`from_number` are
    inputs to that resolution and carry no authority of their own: caller ID
    is spoofable and the called number is an untrusted string on the wire
    (Phase 0 report section 14.1).
    """

    type: CallEventType
    call_ref: CallRef
    direction: CallDirection
    from_number: str | None = None
    to_number: str | None = None
    hangup_cause: HangupCause | None = None
    digit: str | None = None
    related_call_ref: CallRef | None = None


@dataclass(frozen=True, slots=True)
class OriginateRequest:
    """An outbound call request.

    Carries no tenant identifier: the caller has already created the call
    session server-side under a verified tenant context, and the adapter's job
    is to place the call, not to decide whose call it is.
    """

    to_number: str
    from_number: str
    timeout_seconds: int = 30
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """A media format. Defaults match the realistic PSTN leg: 8 kHz mono
    signed 16-bit little-endian PCM."""

    encoding: str = "pcm_s16le"
    sample_rate: int = 8000
    channels: int = 1


@dataclass(frozen=True, slots=True)
class StreamHealth:
    """A media stream's observable condition, for the reconciliation and
    alerting paths in Phase 0 report section 17."""

    attached: bool
    frames_sent: int
    frames_received: int
    last_receive_monotonic: float | None = None


@runtime_checkable
class TelephonyProvider(Protocol):
    """Call control. One implementation is planned
    (`FreeSwitchTelephonyProvider`); this is not a portability promise, it is
    how FreeSWITCH is kept out of the domain."""

    async def originate(self, request: OriginateRequest) -> CallRef: ...

    async def answer(self, call_ref: CallRef) -> None: ...

    async def hangup(self, call_ref: CallRef, cause: HangupCause = HangupCause.NORMAL) -> None: ...

    async def bridge(self, call_ref: CallRef, other_call_ref: CallRef) -> None: ...

    async def transfer(self, call_ref: CallRef, destination: str) -> CallRef: ...

    async def hold(self, call_ref: CallRef) -> None: ...

    async def unhold(self, call_ref: CallRef) -> None: ...

    async def send_dtmf(self, call_ref: CallRef, digits: str) -> None: ...

    async def start_recording(self, call_ref: CallRef) -> None: ...

    async def stop_recording(self, call_ref: CallRef) -> None: ...

    def events(self) -> AsyncIterator[CallEvent]:
        """The normalized lifecycle event stream for every call this provider
        controls. Demultiplexing by `call_ref` is the caller's job."""
        ...


@runtime_checkable
class MediaStream(Protocol):
    """One call leg's bidirectional audio stream.

    Owned by the `MediaProvider`, never by a `ConversationEngine` (ADR-0006
    point 9): the engine consumes and produces frames, it does not hold a
    socket.
    """

    @property
    def format(self) -> AudioFormat: ...

    async def send(self, frame: bytes) -> None: ...

    def receive(self) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


@runtime_checkable
class MediaProvider(Protocol):
    """Media transport: attach, detach, negotiate, report health."""

    def supported_formats(self) -> tuple[AudioFormat, ...]: ...

    async def attach(self, call_ref: CallRef, fmt: AudioFormat | None = None) -> MediaStream:
        """Attach a bidirectional stream to a call leg.

        Raises `UnsupportedFormatError` if `fmt` cannot be provided, and
        `TransportError` if the attach itself fails.
        """
        ...

    async def detach(self, call_ref: CallRef) -> None: ...

    def health(self, call_ref: CallRef) -> StreamHealth: ...
