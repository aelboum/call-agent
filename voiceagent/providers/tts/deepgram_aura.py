"""Deepgram Aura streaming TTS: the second `TtsProvider` adapter (Phase 2.3
brief section 6: "design the adapter boundary for... one additional
provider where practical"). Deepgram offers both STT (Nova, already this
product's first STT vendor -- `voiceagent.providers.stt.deepgram`) and TTS
(Aura) behind the same account/API-key family, which makes it a practical,
real second TTS vendor to implement fully rather than stub.

Deliberately illustrates the *other* case from
`voiceagent.providers.tts.elevenlabs`: Aura's REST streaming endpoint
accepts `encoding=linear16&sample_rate=8000` directly, so this adapter does
**zero** audio conversion -- no resample, no codec decode. Some vendors need
a boundary conversion (ElevenLabs, mu-law), some do not (Aura); the
canonical product audio contract is what makes both cases look identical to
`PipelinedEngine`.

No `deepgram-sdk` dependency -- direct HTTP only, via `httpx`, exactly like
`voiceagent.providers.stt.deepgram`.
"""

from __future__ import annotations

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

__all__ = ["DeepgramAuraTtsConfig", "DeepgramAuraTtsProvider", "create_deepgram_aura_tts_provider"]

_DEFAULT_ENDPOINT = "https://api.deepgram.com/v1/speak"
_DEFAULT_MODEL = "aura-2-thalia-en"
_SECRET_NAME = "DEEPGRAM_API_KEY"  # noqa: S105 -- a secret *name*, not a secret value; shared with the STT adapter (one Deepgram account covers both products).


class DeepgramAuraTtsConfig(BaseModel):
    """The typed shape of `EngineComponentConfig.config` for
    `provider: "deepgram_aura"`."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str = _DEFAULT_ENDPOINT
    sample_rate: int = 8000
    encoding: str = "linear16"
    timeout_seconds: float = 15.0


class DeepgramAuraTtsProvider:
    """One `TtsProvider` bound to one API key and configuration. Aura
    selects a voice through its `model` query parameter (e.g.
    `aura-2-thalia-en` names both the model *and* the voice) -- so
    `VoiceRef.voice_id` maps directly onto it rather than a separate
    parameter."""

    def __init__(self, *, api_key: str, config: DeepgramAuraTtsConfig) -> None:
        self._api_key = api_key
        self._config = config

    async def synthesize(self, text: str, voice: VoiceRef | None) -> AsyncIterator[AudioOut]:
        model = voice.voice_id if voice is not None else _DEFAULT_MODEL
        params = {
            "model": model,
            "encoding": self._config.encoding,
            "sample_rate": str(self._config.sample_rate),
            "container": "none",
        }
        headers = {"Authorization": f"Token {self._api_key}"}
        try:
            async with (
                httpx.AsyncClient(timeout=self._config.timeout_seconds) as client,
                client.stream(
                    "POST",
                    self._config.endpoint,
                    params=params,
                    json={"text": text},
                    headers=headers,
                ) as response,
            ):
                if response.status_code != 200:
                    body_text = (await response.aread()).decode("utf-8", errors="replace")
                    raise self._status_error(response.status_code, body_text)
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield AudioOut(frame=chunk, sample_rate=self._config.sample_rate)
        except httpx.TimeoutException as exc:
            raise EngineException(
                EngineErrorCode.TRANSIENT, "deepgram_aura: request timed out"
            ) from exc
        except httpx.TransportError as exc:
            raise EngineException(
                EngineErrorCode.TRANSIENT, f"deepgram_aura: transport failure: {exc}"
            ) from exc

    def _status_error(self, status: int, body_text: str) -> EngineException:
        if status in (401, 403):
            return EngineException(EngineErrorCode.AUTH, f"deepgram_aura: auth rejected ({status})")
        if status == 429:
            return EngineException(EngineErrorCode.RATE_LIMIT, "deepgram_aura: rate limited")
        if status in (400, 422):
            return EngineException(
                EngineErrorCode.INVALID_REQUEST, f"deepgram_aura: invalid request ({status})"
            )
        if status >= 500:
            return EngineException(
                EngineErrorCode.PROVIDER_DOWN, f"deepgram_aura: server error ({status})"
            )
        return EngineException(
            EngineErrorCode.TRANSIENT,
            f"deepgram_aura: unexpected status {status}: {body_text[:200]}",
        )


def create_deepgram_aura_tts_provider(config: Mapping[str, object]) -> DeepgramAuraTtsProvider:
    parsed = DeepgramAuraTtsConfig.model_validate(dict(config))
    try:
        api_key = get_secrets_provider().get_required(_SECRET_NAME)
    except SecretNotFoundError as exc:
        raise EngineException(
            EngineErrorCode.AUTH, f"deepgram_aura: {_SECRET_NAME} is not configured"
        ) from exc
    return DeepgramAuraTtsProvider(api_key=api_key, config=parsed)
