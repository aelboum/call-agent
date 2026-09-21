"""Telephony boundary (ADR-0002).

`contracts` holds the two product-owned interfaces, `TelephonyProvider` and
`MediaProvider`; `fakes` holds in-memory implementations for tests; the
`freeswitch` subpackage will hold the only real implementation and is
importable from nowhere else.

This package's `__init__` deliberately does not re-export anything from
`freeswitch`: a convenience re-export here would silently defeat the import
fence that keeps FreeSWITCH out of the domain.
"""

from __future__ import annotations

from voiceagent.telephony.contracts import (
    AudioFormat,
    CallDirection,
    CallEvent,
    CallEventType,
    CallRef,
    HangupCause,
    MediaProvider,
    MediaStream,
    OriginateRequest,
    StreamHealth,
    TelephonyError,
    TelephonyProvider,
    TransportError,
    UnsupportedFormatError,
)

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
