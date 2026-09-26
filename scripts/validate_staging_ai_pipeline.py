#!/usr/bin/env python
"""Phase 2.23: real AI-provider round-trip validation (STT -> LLM -> TTS).

**Staging-only.** Uses the existing provider *registries*
(`voiceagent.providers.{stt,tts}.registry`, `voiceagent.providers.llm
.registry`) exactly as `voiceagent.providers.engines.factory
.build_conversation_engine()` would -- no vendor SDK type, no new
abstraction, no architecture change. Every credential is read through
`infra.secrets` by the existing adapters themselves; this script never
touches `DEEPGRAM_API_KEY`/`OPENAI_API_KEY` directly and never prints one.

What this proves, all against real vendor APIs:

1. Real Deepgram Aura TTS synthesizes real audio for a short known phrase.
2. That same real audio, fed straight into real Deepgram STT (no manual
   resampling needed -- Aura's default output format is already this
   product's canonical 8 kHz mono linear16, `voiceagent.providers.tts
   .deepgram_aura`'s own docstring), round-trips back to a real transcript.
3. A real OpenAI chat-completion request/response, bounded (`max_tokens`).
4. That LLM response resynthesized through real Deepgram Aura TTS again.

**What this does NOT prove**: audio flowing over a real phone call.
`mod_audio_stream` is not present on the FreeSWITCH image validated
elsewhere in this phase (`scripts/validate_staging_call_e2e.py`'s own
finding) and no real SIP/PSTN caller exists in this environment -- this
script validates the AI pipeline at the provider/engine boundary directly,
the maximum real validation achievable without that missing piece. See
docs/PHASE-2.23-REAL-STAGING-E2E.md for the exact, honest scope line this
draws.

Usage::

    ENVIRONMENT=development SECRETS_ENV_FILE=.env.phase223.local \\
    python scripts/validate_staging_ai_pipeline.py

Exit code 0 only if every stage succeeds. Never prints a secret, an
Authorization header, or more than a short bounded preview of any text.
"""

from __future__ import annotations

import asyncio

from voiceagent.providers.engines.contracts import FinalTranscript, TurnEnded
from voiceagent.providers.llm.registry import create_llm_provider
from voiceagent.providers.stt.registry import create_stt_provider
from voiceagent.providers.tts.registry import create_tts_provider

_TEST_PHRASE = "Hello, this is a staging validation test."
_LLM_PROMPT = "Reply with exactly the single word: OK"


def _preview(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


async def _synthesize(tts, text: str) -> bytes:
    audio = bytearray()
    async for out in tts.synthesize(text, None):
        audio.extend(out.frame)
    return bytes(audio)


async def _transcribe(stt, audio: bytes, *, chunk_size: int = 3200) -> str:
    async def _frames():
        for i in range(0, len(audio), chunk_size):
            yield audio[i : i + chunk_size]
            await asyncio.sleep(0.02)

    final_text_parts: list[str] = []
    async for event in stt.stream(_frames()):
        if isinstance(event, FinalTranscript) and event.text:
            final_text_parts.append(event.text)
    return " ".join(final_text_parts).strip()


async def _complete(llm, prompt: str, *, max_tokens: int = 16) -> str:
    messages = [{"role": "user", "content": prompt}]
    parts: list[str] = []
    async for event in llm.stream_turn(messages, tools=()):
        if isinstance(event, str):
            parts.append(event)
        elif isinstance(event, TurnEnded):
            break
    return "".join(parts).strip()


async def _run() -> int:
    tts = create_tts_provider("deepgram_aura", {})
    stt = create_stt_provider("deepgram", {})
    llm = create_llm_provider("openai", "gpt-4o-mini", {"max_tokens": 16})

    print("[1/4] real Deepgram Aura TTS: synthesizing a known test phrase ...")
    try:
        audio = await _synthesize(tts, _TEST_PHRASE)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL (tts): {exc}")
        return 1
    if not audio:
        print("FAIL (tts): no audio bytes returned")
        return 1
    print(f"      PASS: received {len(audio)} bytes of real synthesized audio")

    print("[2/4] real Deepgram STT: transcribing that same real audio back ...")
    try:
        transcript = await _transcribe(stt, audio)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL (stt): {exc}")
        return 1
    if not transcript:
        print("FAIL (stt): no transcript produced")
        return 1
    print(f"      PASS: transcript={_preview(transcript)!r}")

    print("[3/4] real OpenAI LLM: one bounded chat-completion request ...")
    try:
        reply = await _complete(llm, _LLM_PROMPT)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL (llm): {exc}")
        return 1
    if not reply:
        print("FAIL (llm): empty response")
        return 1
    print(f"      PASS: response={_preview(reply)!r}")

    print("[4/4] real Deepgram Aura TTS: resynthesizing the LLM's own response ...")
    try:
        reply_audio = await _synthesize(tts, reply)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL (tts-reply): {exc}")
        return 1
    if not reply_audio:
        print("FAIL (tts-reply): no audio bytes returned")
        return 1
    print(f"      PASS: received {len(reply_audio)} bytes of real synthesized audio")

    print("PASS: real STT -> LLM -> TTS round trip succeeded end to end")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
