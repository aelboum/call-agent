"""A minimal, dependency-free G.711 mu-law decoder.

Python's `audioop` module (the stdlib's traditional home for this) was
removed in Python 3.12/3.13 (PEP 594) -- this repository targets 3.13
(`pyproject.toml`), so it is unavailable. The decode itself is a fixed,
small, well-defined transform (ITU-T G.711), not something that benefits
from a dependency, and it is confined to exactly the one adapter that needs
it (`voiceagent.providers.tts.elevenlabs`, which requests `ulaw_8000` -- a
telephony-native format requiring zero *resampling*, only this codec
conversion -- to avoid ever resampling the canonical 8 kHz rate at all;
Phase 2.3 brief section 13: "do not silently resample multiple times").
"""

from __future__ import annotations

__all__ = ["ulaw_to_pcm16"]

_BIAS = 0x84


def ulaw_to_pcm16(data: bytes) -> bytes:
    """Decode mu-law-encoded bytes into signed 16-bit little-endian PCM."""
    out = bytearray(len(data) * 2)
    for i, raw_byte in enumerate(data):
        byte = ~raw_byte & 0xFF
        sign = byte & 0x80
        exponent = (byte >> 4) & 0x07
        mantissa = byte & 0x0F
        sample = ((mantissa << 3) + _BIAS) << exponent
        sample -= _BIAS
        if sign:
            sample = -sample
        sample = max(-32768, min(32767, sample))
        out[2 * i : 2 * i + 2] = sample.to_bytes(2, "little", signed=True)
    return bytes(out)
