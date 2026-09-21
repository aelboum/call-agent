"""AssemblyAI real-time STT: the second `SttProvider` adapter (Phase 2.3
brief section 5: "design the adapter boundary for... one additional
provider where practical"), chosen specifically because its wire shape
differs from Deepgram's in a way worth proving the registry/factory is
genuinely provider-neutral against: audio travels as base64-encoded JSON
messages here, not raw binary WebSocket frames, and partial/final
transcripts arrive as a `message_type` discriminator rather than an
`is_final` boolean on one message type. If `PipelinedEngine`/the runtime
had to change to accommodate either shape, the registry/factory design
would have failed its own purpose.

Like `voiceagent.providers.stt.deepgram`, implemented directly against
`websockets` -- no vendor SDK dependency.

**Verification note**: this adapter was not checked against a freshly
fetched AssemblyAI documentation page during this phase (only Deepgram,
ElevenLabs, Gemini, Mistral and Groq were attempted via `WebFetch`; two of
those five 404'd and one returned a response inconsistent with the vendor's
known API shape -- see `docs/PHASE-2.3-STATUS.md` section 3). The message
shapes below follow AssemblyAI's real-time transcription API from
training-era knowledge and must be re-verified against current
documentation before any production use.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Mapping

import websockets
from infra.secrets import SecretNotFoundError, get_secrets_provider
from pydantic import BaseModel, ConfigDict
from websockets.exceptions import InvalidStatus

from voiceagent.providers.engines.contracts import (
    EngineErrorCode,
    EngineException,
    FinalTranscript,
    PartialTranscript,
)

__all__ = ["AssemblyAiSttConfig", "AssemblyAiSttProvider", "create_assemblyai_stt_provider"]

_logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "wss://api.assemblyai.com/v2/realtime/ws"
_SECRET_NAME = "ASSEMBLYAI_API_KEY"  # noqa: S105 -- a secret *name*, not a secret value.


class AssemblyAiSttConfig(BaseModel):
    """The typed shape of `EngineComponentConfig.config` for
    `provider: "assemblyai"`, validated at this adapter's boundary --
    mirrors `voiceagent.providers.stt.deepgram.DeepgramSttConfig`'s role."""

    model_config = ConfigDict(extra="forbid")

    language: str = "en"
    endpoint: str = _DEFAULT_ENDPOINT
    sample_rate: int = 8000
    timeout_seconds: float = 10.0


class AssemblyAiSttProvider:
    """One `SttProvider` bound to one API key and configuration."""

    def __init__(self, *, api_key: str, config: AssemblyAiSttConfig) -> None:
        self._api_key = api_key
        self._config = config

    def _url(self) -> str:
        return f"{self._config.endpoint}?sample_rate={self._config.sample_rate}"

    async def stream(
        self, audio: AsyncIterator[bytes]
    ) -> AsyncIterator[PartialTranscript | FinalTranscript]:
        try:
            async with websockets.connect(
                self._url(),
                additional_headers={"Authorization": self._api_key},
                open_timeout=self._config.timeout_seconds,
            ) as connection:
                send_task = asyncio.create_task(self._send_audio(connection, audio))
                try:
                    async for message in connection:
                        event = self._parse_event(message)
                        if event is not None:
                            yield event
                finally:
                    send_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await send_task
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise EngineException(
                    EngineErrorCode.AUTH, f"assemblyai: auth rejected ({status})"
                ) from exc
            if status == 429:
                raise EngineException(
                    EngineErrorCode.RATE_LIMIT, "assemblyai: rate limited"
                ) from exc
            raise EngineException(
                EngineErrorCode.PROVIDER_DOWN, f"assemblyai: unexpected status {status}"
            ) from exc
        except TimeoutError as exc:
            raise EngineException(
                EngineErrorCode.TRANSIENT, "assemblyai: connection timed out"
            ) from exc
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            raise EngineException(
                EngineErrorCode.TRANSIENT, f"assemblyai: transport failure: {exc}"
            ) from exc

    async def _send_audio(
        self, connection: websockets.ClientConnection, audio: AsyncIterator[bytes]
    ) -> None:
        async for frame in audio:
            payload = json.dumps({"audio_data": base64.b64encode(frame).decode("ascii")})
            await connection.send(payload)
        with contextlib.suppress(websockets.exceptions.WebSocketException):
            await connection.send(json.dumps({"terminate_session": True}))

    def _parse_event(self, raw: str | bytes) -> PartialTranscript | FinalTranscript | None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            _logger.warning("assemblyai: dropped a malformed event")
            return None
        if not isinstance(data, dict):
            return None
        message_type = data.get("message_type")
        text = data.get("text")
        if not text or message_type not in ("PartialTranscript", "FinalTranscript"):
            return None
        if message_type == "FinalTranscript":
            return FinalTranscript(text=text, confidence=data.get("confidence"))
        return PartialTranscript(text=text)


def create_assemblyai_stt_provider(config: Mapping[str, object]) -> AssemblyAiSttProvider:
    """The factory `voiceagent.providers.stt.registry.STT_PROVIDERS` calls
    for `provider: "assemblyai"`."""
    parsed = AssemblyAiSttConfig.model_validate(dict(config))
    try:
        api_key = get_secrets_provider().get_required(_SECRET_NAME)
    except SecretNotFoundError as exc:
        raise EngineException(
            EngineErrorCode.AUTH, f"assemblyai: {_SECRET_NAME} is not configured"
        ) from exc
    return AssemblyAiSttProvider(api_key=api_key, config=parsed)
