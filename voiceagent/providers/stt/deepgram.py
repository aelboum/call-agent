"""Deepgram streaming STT (ADR-0009 point 2), over Deepgram's documented
real-time WebSocket API (`wss://api.deepgram.com/v1/listen`), implemented
directly against `websockets` rather than the `deepgram-sdk` package -- no
vendor SDK becomes a dependency, and the wire protocol (a persistent
WebSocket, raw binary audio frames in, JSON `Results` events out) is stable
and small enough that a direct client is less surface than a full SDK for
what this adapter needs (Phase 2.3 brief section 24's "if direct HTTP/
WebSocket implementation is cleaner... acceptable, document the choice").

Everything Deepgram-shaped -- the WebSocket URL, the `Authorization: Token`
header, the `Results` message envelope, `is_final`/`speech_final`, the
`CloseStream` control message -- is confined to this module. Nothing above
`voiceagent.providers.stt.registry` ever sees it; `SttProvider.stream()`
(`voiceagent.providers.engines.contracts`) is the only surface this module
exposes upward.

Audio: requested at exactly the canonical product format
(`voiceagent.telephony.contracts.AudioFormat`'s default -- 8 kHz mono
`pcm_s16le`/linear16), so no resampling happens in this adapter at all;
Deepgram accepts that encoding/rate natively for telephony-shaped audio
(ADR-0009's own research note: "telephony-native 8 kHz input").

**Verification note** (Phase 2.3 brief section 2 / `docs/PHASE-2.3-STATUS.md`
section 3): live re-verification of `developers.deepgram.com/docs/streaming`
via `WebFetch` during this phase returned a 404 for the specific URL tried.
The message shapes and parameters below follow Deepgram's long-stable,
well-documented real-time API from training-era knowledge, not a
freshly-fetched page -- flagged explicitly as a known limitation in
`docs/PHASE-2.3-STATUS.md` rather than presented as freshly verified.
"""

from __future__ import annotations

import asyncio
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

__all__ = ["DeepgramSttConfig", "DeepgramSttProvider", "create_deepgram_stt_provider"]

_logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "wss://api.deepgram.com/v1/listen"
_SECRET_NAME = "DEEPGRAM_API_KEY"  # noqa: S105 -- a secret *name*, not a secret value.


class DeepgramSttConfig(BaseModel):
    """The typed shape of `EngineComponentConfig.config` for
    `provider: "deepgram"` (an STT slot in `AgentConfig.engine`,
    `voiceagent.agents.config`). Validated at this adapter's own boundary --
    the durable `AgentVersion.config` field stays the pre-existing, already
    approved `dict[str, object]` bag (Phase 2.0 report §9.3); this model is
    what keeps that bag from ever being read as an untyped blob past this
    point (Phase 2.3 brief: "Provider-specific optional settings should use
    typed configuration structures")."""

    model_config = ConfigDict(extra="forbid")

    model: str = "nova-3"
    language: str = "en"
    endpoint: str = _DEFAULT_ENDPOINT
    sample_rate: int = 8000
    encoding: str = "linear16"
    channels: int = 1
    timeout_seconds: float = 10.0
    interim_results: bool = True


class DeepgramSttProvider:
    """One `SttProvider` bound to one API key and configuration. Opens one
    fresh WebSocket per `stream()` call -- `SttProvider` is stateless apart
    from its configuration, exactly like `PipelinedEngineSession` expects."""

    def __init__(self, *, api_key: str, config: DeepgramSttConfig) -> None:
        self._api_key = api_key
        self._config = config

    def _url(self) -> str:
        params = (
            f"model={self._config.model}"
            f"&language={self._config.language}"
            f"&encoding={self._config.encoding}"
            f"&sample_rate={self._config.sample_rate}"
            f"&channels={self._config.channels}"
            f"&interim_results={'true' if self._config.interim_results else 'false'}"
        )
        return f"{self._config.endpoint}?{params}"

    async def stream(
        self, audio: AsyncIterator[bytes]
    ) -> AsyncIterator[PartialTranscript | FinalTranscript]:
        try:
            async with websockets.connect(
                self._url(),
                additional_headers={"Authorization": f"Token {self._api_key}"},
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
                    EngineErrorCode.AUTH, f"deepgram: auth rejected ({status})"
                ) from exc
            if status == 429:
                raise EngineException(EngineErrorCode.RATE_LIMIT, "deepgram: rate limited") from exc
            raise EngineException(
                EngineErrorCode.PROVIDER_DOWN, f"deepgram: unexpected status {status}"
            ) from exc
        except TimeoutError as exc:
            raise EngineException(
                EngineErrorCode.TRANSIENT, "deepgram: connection timed out"
            ) from exc
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            raise EngineException(
                EngineErrorCode.TRANSIENT, f"deepgram: transport failure: {exc}"
            ) from exc

    async def _send_audio(
        self, connection: websockets.ClientConnection, audio: AsyncIterator[bytes]
    ) -> None:
        async for frame in audio:
            await connection.send(frame)
        with contextlib.suppress(websockets.exceptions.WebSocketException):
            await connection.send(json.dumps({"type": "CloseStream"}))

    def _parse_event(self, raw: str | bytes) -> PartialTranscript | FinalTranscript | None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            _logger.warning("deepgram: dropped a malformed event")
            return None
        if not isinstance(data, dict) or data.get("type") != "Results":
            return None
        try:
            alternative = data["channel"]["alternatives"][0]
            text = alternative["transcript"]
        except (KeyError, IndexError, TypeError):
            _logger.warning("deepgram: dropped a Results event with an unexpected shape")
            return None
        if not text:
            return None
        if data.get("is_final"):
            return FinalTranscript(text=text, confidence=alternative.get("confidence"))
        return PartialTranscript(text=text)


def create_deepgram_stt_provider(config: Mapping[str, object]) -> DeepgramSttProvider:
    """The factory `voiceagent.providers.stt.registry.STT_PROVIDERS` calls
    for `provider: "deepgram"`. Reads the API key through
    `infra.secrets.get_secrets_provider()` -- never through
    `voiceagent.config.settings`, which carries no secret field by design --
    at call time, so no key is ever cached on a long-lived settings object."""
    parsed = DeepgramSttConfig.model_validate(dict(config))
    try:
        api_key = get_secrets_provider().get_required(_SECRET_NAME)
    except SecretNotFoundError as exc:
        raise EngineException(
            EngineErrorCode.AUTH, f"deepgram: {_SECRET_NAME} is not configured"
        ) from exc
    return DeepgramSttProvider(api_key=api_key, config=parsed)
