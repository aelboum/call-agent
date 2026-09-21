"""`voiceagent.providers.tts._ulaw` -- the one audio conversion this phase's
adapters perform (ElevenLabs' `ulaw_8000` output -> the canonical
`pcm_s16le`, per `voiceagent.telephony.contracts.AudioFormat`'s default)."""

from __future__ import annotations

from voiceagent.providers.tts._ulaw import ulaw_to_pcm16


def test_output_is_twice_the_length_of_the_input() -> None:
    assert len(ulaw_to_pcm16(bytes([0x00, 0xFF, 0x7F]))) == 6


def test_is_deterministic() -> None:
    data = bytes(range(0, 256))
    assert ulaw_to_pcm16(data) == ulaw_to_pcm16(data)


def test_the_canonical_silence_byte_decodes_near_zero() -> None:
    """0xFF is mu-law's own encoding of (positive) digital silence."""
    decoded = ulaw_to_pcm16(bytes([0xFF] * 4))
    for i in range(0, len(decoded), 2):
        sample = int.from_bytes(decoded[i : i + 2], "little", signed=True)
        assert abs(sample) < 50


def test_different_bytes_decode_to_different_samples() -> None:
    low = ulaw_to_pcm16(bytes([0x00]))
    high = ulaw_to_pcm16(bytes([0x7F]))
    assert low != high
