"""ElevenLabs streaming TTS (ADR-0009 point 4), over ElevenLabs' HTTP
streaming endpoint (`POST /v1/text-to-speech/{voice_id}/stream`) -- chosen
over the WebSocket `stream-input` endpoint deliberately: this phase's brief
prefers the simpler transport when it is sufficient, and one request-per-
turn HTTP streaming is (turn-level synthesis is already how
`PipelinedEngineSession._turn()` calls `TtsProvider.synthesize()` -- once
per assistant turn, not token-by-token). A WebSocket adapter remains a
possible later optimization for intra-turn latency, noted as a deferred
option in `docs/PHASE-2.3-STATUS.md` rather than built now.

Requests `output_format=ulaw_8000` -- ElevenLabs' own telephony-native
output -- so the only conversion needed is a mu-law -> PCM16 codec decode
(`voiceagent.providers.tts._ulaw`), never a resample: the canonical product
audio contract is 8 kHz throughout, and `ulaw_8000` already is.

**Zero-retention, on every request, unconditionally** (ADR-0009 point 8:
""configured not to train" is an adapter-level, per-call obligation, not an
assumption... the adapter must set it on every request with no code path
that omits it"): `enable_logging=false` is set in `_synthesize_once()`'s
query parameters with no branch that skips it.

No `elevenlabs` SDK dependency -- direct HTTP only, via `httpx`.

**Verification note**: `WebFetch` against ElevenLabs' TTS-streaming API
reference during this phase 404'd on the specific URL tried (see
`docs/PHASE-2.3-STATUS.md` section 3). The request/response shape below
follows ElevenLabs' long-documented streaming TTS API from training-era
knowledge, flagged as unverified-this-session rather than presented as
freshly checked.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping

import httpx
from infra.secrets import SecretNotFoundError, get_secrets_provider
from pydantic import BaseModel, ConfigDict

from voiceagent.providers.engines.contracts import (
    AudioOut,
    EngineErrorCode,
    EngineException,
    VoiceRef,
)
from voiceagent.providers.tts._ulaw import ulaw_to_pcm16

__all__ = ["ElevenLabsTtsConfig", "ElevenLabsTtsProvider", "create_elevenlabs_tts_provider"]

_logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://api.elevenlabs.io/v1"
_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs' publicly documented sample voice.
_SECRET_NAME = "ELEVENLABS_API_KEY"  # noqa: S105 -- a secret *name*, not a secret value.


class ElevenLabsTtsConfig(BaseModel):
    """The typed shape of `EngineComponentConfig.config` for
    `provider: "elevenlabs"` (a TTS slot in `AgentConfig.engine`)."""

    model_config = ConfigDict(extra="forbid")

    model: str = "eleven_turbo_v2_5"
    endpoint: str = _DEFAULT_ENDPOINT
    timeout_seconds: float = 15.0


class ElevenLabsTtsProvider:
    """One `TtsProvider` bound to one API key and configuration.

    Cancellation: `synthesize()` is an async generator wrapping an `httpx`
    streaming context manager; when `PipelinedEngineSession.interrupt()`
    cancels the in-flight turn task mid-iteration, the `async for`
    consuming this generator raises `GeneratorExit` into it at the
    `yield`, which unwinds through the `async with client.stream(...)`
    block and closes the HTTP connection -- no separate cancellation
    plumbing is needed here.
    """

    def __init__(self, *, api_key: str, config: ElevenLabsTtsConfig) -> None:
        self._api_key = api_key
        self._config = config

    async def synthesize(self, text: str, voice: VoiceRef | None) -> AsyncIterator[AudioOut]:
        voice_id = voice.voice_id if voice is not None else _DEFAULT_VOICE_ID
        url = f"{self._config.endpoint}/text-to-speech/{voice_id}/stream"
        params = {"output_format": "ulaw_8000", "enable_logging": "false"}
        body: dict[str, object] = {"text": text, "model_id": self._config.model}
        if voice is not None and voice.settings:
            body["voice_settings"] = dict(voice.settings)
        headers = {"xi-api-key": self._api_key}

        try:
            async with (
                httpx.AsyncClient(timeout=self._config.timeout_seconds) as client,
                client.stream("POST", url, params=params, json=body, headers=headers) as response,
            ):
                if response.status_code != 200:
                    body_text = (await response.aread()).decode("utf-8", errors="replace")
                    raise self._status_error(response.status_code, body_text)
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    yield AudioOut(frame=ulaw_to_pcm16(chunk), sample_rate=8000)
        except httpx.TimeoutException as exc:
            raise EngineException(
                EngineErrorCode.TRANSIENT, "elevenlabs: request timed out"
            ) from exc
        except httpx.TransportError as exc:
            raise EngineException(
                EngineErrorCode.TRANSIENT, f"elevenlabs: transport failure: {exc}"
            ) from exc

    def _status_error(self, status: int, body_text: str) -> EngineException:
        if status in (401, 403):
            return EngineException(EngineErrorCode.AUTH, f"elevenlabs: auth rejected ({status})")
        if status == 429:
            return EngineException(EngineErrorCode.RATE_LIMIT, "elevenlabs: rate limited")
        if status in (400, 422):
            return EngineException(
                EngineErrorCode.INVALID_REQUEST, f"elevenlabs: invalid request ({status})"
            )
        if status >= 500:
            return EngineException(
                EngineErrorCode.PROVIDER_DOWN, f"elevenlabs: server error ({status})"
            )
        return EngineException(
            EngineErrorCode.TRANSIENT, f"elevenlabs: unexpected status {status}: {body_text[:200]}"
        )


def create_elevenlabs_tts_provider(config: Mapping[str, object]) -> ElevenLabsTtsProvider:
    parsed = ElevenLabsTtsConfig.model_validate(dict(config))
    try:
        api_key = get_secrets_provider().get_required(_SECRET_NAME)
    except SecretNotFoundError as exc:
        raise EngineException(
            EngineErrorCode.AUTH, f"elevenlabs: {_SECRET_NAME} is not configured"
        ) from exc
    return ElevenLabsTtsProvider(api_key=api_key, config=parsed)
