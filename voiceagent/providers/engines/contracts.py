"""The ConversationEngine contract (ADR-0006).

```
ConversationEngine
├── PipelinedEngine(stt, llm, tts)     -- Phase 2
└── RealtimeEngine(provider)           -- Phase 2
```

**This contract is product-owned and framework-free.** No Pipecat type, frame,
processor or exception appears here, and none ever may: the Call Runtime is
written once against this module, which is what makes the runtime rewrite
Phase 0 prohibits structurally impossible. `tests/architecture/` asserts the
absence mechanically, today, while Pipecat is not even installed.

Why one contract over two very different implementation styles: a three-box
STT/LLM/TTS pipeline cannot express a realtime speech-to-speech provider,
where transcription, reasoning, tool-calling and synthesis happen inside one
stateful duplex session. An abstraction that assumed the pipeline shape would
have to bolt realtime on later -- the rewrite. So the runtime depends on the
event stream below, and each engine maps its own mechanism onto it.

Boundaries this contract keeps:

* **No tenancy.** An engine receives a resolved, immutable session
  configuration and has no tenant concept. Tenant isolation lives above it.
* **No transport.** The media socket belongs to the `MediaProvider`
  (ADR-0002 amendment); an engine consumes and produces PCM frames.
* **No tool execution.** An engine *requests* a tool call and *receives* a
  result. Authorization, validation, idempotency and audit happen in the Tool
  Gateway (ADR-0003), never in an engine.
* **No database.** Ever.

Phase 1 defines the contract and a deterministic fake. No real engine exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

__all__ = [
    "AudioOut",
    "ConversationEngine",
    "EngineError",
    "EngineErrorCode",
    "EngineEvent",
    "EngineException",
    "EngineSession",
    "EngineSessionConfig",
    "FinalTranscript",
    "LlmProvider",
    "PartialTranscript",
    "SpeechEnded",
    "SpeechStarted",
    "SttProvider",
    "ToolCallRequested",
    "ToolResult",
    "ToolSpec",
    "TtsProvider",
    "TurnEnded",
    "UsageReported",
    "VoiceCatalog",
    "VoiceRef",
]


class EngineErrorCode(StrEnum):
    """Provider-agnostic error taxonomy.

    The runtime's fallback policy is written against these five codes, so a
    provider swap never changes how a failure is handled. An adapter maps its
    vendor's errors onto them and lets nothing else escape.
    """

    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    INVALID_REQUEST = "invalid_request"
    PROVIDER_DOWN = "provider_down"


class EngineException(Exception):
    """Raised by an engine for a failure that is not recoverable in-session."""

    def __init__(self, code: EngineErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class VoiceRef:
    """A voice as a *reference*, never an inlined vendor value.

    Stored this way in a published agent version so that changing provider is
    a configuration migration, not a code change (Phase 0 report section 11.3).
    """

    provider: str
    voice_id: str
    settings: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool as the model sees it: a name, a description and a strict JSON
    Schema. The engine never learns what the tool does, who may call it, or
    what it touches -- that is the Tool Gateway's business."""

    name: str
    description: str
    input_schema: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of a tool call, handed back to the engine.

    A failure is a *value*, not an exception, at the model boundary: the agent
    can then say "I couldn't book that, may I take a message?" instead of
    going silent. Internal detail never crosses this line.
    """

    call_id: str
    value: Mapping[str, object] | None = None
    error_code: str | None = None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class EngineSessionConfig:
    """Everything an engine needs for one call, resolved before it starts.

    Derived from an immutable published agent version (ADR-0004) and loaded
    once for the life of the call, so a draft edit mid-call cannot reach it.
    """

    instructions: str
    greeting: str | None = None
    language: str = "en"
    voice: VoiceRef | None = None
    tools: Sequence[ToolSpec] = ()
    input_sample_rate: int = 8000
    output_sample_rate: int = 8000


# -- events -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioOut:
    """A frame of agent speech to play to the caller."""

    frame: bytes
    sample_rate: int


@dataclass(frozen=True, slots=True)
class PartialTranscript:
    text: str


@dataclass(frozen=True, slots=True)
class FinalTranscript:
    text: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class SpeechStarted:
    """The caller began speaking. The runtime treats this as barge-in."""

    speaker: str = "caller"


@dataclass(frozen=True, slots=True)
class SpeechEnded:
    speaker: str = "caller"


@dataclass(frozen=True, slots=True)
class ToolCallRequested:
    """The model asked for a tool. `call_id` correlates the later
    `ToolResult`, and is also the idempotency key component the Tool Gateway
    uses so a retry cannot double-book."""

    call_id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TurnEnded:
    """The agent finished a turn and is listening."""


@dataclass(frozen=True, slots=True)
class UsageReported:
    """Consumption in provider-neutral units (e.g. `{"stt_seconds": 3.2}`),
    so metering does not change when a provider does. A cancelled synthesis
    still reports what it consumed."""

    units: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class EngineError:
    """A failure the session survived, surfaced rather than raised."""

    code: EngineErrorCode
    message: str


type EngineEvent = (
    AudioOut
    | PartialTranscript
    | FinalTranscript
    | SpeechStarted
    | SpeechEnded
    | ToolCallRequested
    | TurnEnded
    | UsageReported
    | EngineError
)


# -- the contract -----------------------------------------------------------


@runtime_checkable
class EngineSession(Protocol):
    """One conversation, for the life of one call."""

    async def send_audio(self, frame: bytes) -> None:
        """Feed one frame of caller audio. Must not block the caller's
        event loop on network I/O."""
        ...

    async def interrupt(self) -> None:
        """Barge-in: stop synthesis mid-frame and reconcile context.
        Idempotent, and safe to call at any point in a turn."""
        ...

    async def submit_tool_result(self, result: ToolResult) -> None: ...

    async def close(self) -> None:
        """Idempotent. After it returns, `events()` completes."""
        ...

    def events(self) -> AsyncIterator[EngineEvent]: ...


@runtime_checkable
class ConversationEngine(Protocol):
    """Starts sessions. Stateless apart from its provider configuration."""

    async def start(self, config: EngineSessionConfig) -> EngineSession: ...


# -- component providers, composed by PipelinedEngine in Phase 2 ------------


@runtime_checkable
class SttProvider(Protocol):
    """Streaming speech-to-text."""

    def stream(
        self, audio: AsyncIterator[bytes]
    ) -> AsyncIterator[PartialTranscript | FinalTranscript]: ...


@runtime_checkable
class LlmProvider(Protocol):
    """Streaming chat completion with tool calling.

    Yields text deltas as `str` and tool calls as `ToolCallRequested`; a
    `TurnEnded` closes the turn.
    """

    def stream_turn(
        self, messages: Sequence[Mapping[str, object]], tools: Sequence[ToolSpec]
    ) -> AsyncIterator[str | ToolCallRequested | TurnEnded]: ...


@runtime_checkable
class TtsProvider(Protocol):
    """Streaming synthesis. Cancellation must stop mid-frame."""

    def synthesize(self, text: str, voice: VoiceRef) -> AsyncIterator[AudioOut]: ...


@runtime_checkable
class VoiceCatalog(Protocol):
    """Voice discovery, used at agent-version publish time to validate that a
    configured voice actually exists before a caller ever hears the result."""

    async def list_voices(self) -> Sequence[VoiceRef]: ...
