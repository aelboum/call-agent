"""`PipelinedEngine` (ADR-0006, ADR-0009 point 1): a `ConversationEngine`
composed from `SttProvider` + `LlmProvider` + `TtsProvider`, exactly as
`voiceagent.providers.engines.contracts` already specifies.

```text
audio input -> STT -> transcript -> LLM -> assistant text -> TTS -> audio output
```

Framework-free: nothing here imports Pipecat, and this module is one of the
sources `tests/architecture/test_import_boundaries.py`'s Pipecat fence
already covers by prefix (`voiceagent.providers.engines.*` outside
`voiceagent.providers.engines.pipecat`). A Pipecat-backed `PipelinedEngine`
remains a possible *future* implementation of this same class shape (ADR-0006
point 2) -- this one proves the contract with no framework at all, which is
what keeps Pipecat optional rather than load-bearing (ADR-0006 point 7).

Turn ownership is a single, cancellable `asyncio.Task` per in-flight LLM/TTS
turn (`_turn_task`), kept separate from the STT-consumption task so that
`interrupt()` can cancel a turn mid-synthesis without tearing down the
session's ability to keep hearing the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from voiceagent.providers.engines.contracts import (
    AssistantResponse,
    AudioOut,
    EngineError,
    EngineEvent,
    EngineException,
    EngineSessionConfig,
    FinalTranscript,
    LlmProvider,
    PartialTranscript,
    SttProvider,
    SystemPromptSet,
    ToolCallRequested,
    ToolResult,
    TtsProvider,
    TurnEnded,
)

__all__ = ["PipelinedEngine", "PipelinedEngineSession"]


def _initial_messages(config: EngineSessionConfig) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = [{"role": "system", "content": config.instructions}]
    if config.greeting:
        messages.append({"role": "assistant", "content": config.greeting})
    return messages


class PipelinedEngineSession:
    """One conversation, for the life of one call, driven by three
    independently swappable component providers."""

    def __init__(
        self,
        config: EngineSessionConfig,
        stt: SttProvider,
        llm: LlmProvider,
        tts: TtsProvider,
    ) -> None:
        self.config = config
        self.interrupts = 0
        self.closed = False
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._messages: list[dict[str, object]] = _initial_messages(config)
        self._audio_in: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._events: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
        self._pending_tool_results: dict[str, asyncio.Future[ToolResult]] = {}
        self._turn_task: asyncio.Task[None] | None = None
        # Durable "system"/"assistant" turns (Phase 2.5): emitted once, up
        # front, before any audio has been sent or received -- buffered by
        # `self._events` (an ordinary asyncio.Queue) until a consumer starts
        # draining `events()`, exactly like every other event this session
        # emits.
        self._emit(SystemPromptSet(instructions=config.instructions))
        if config.greeting:
            self._emit(AssistantResponse(text=config.greeting))
        self._stt_task: asyncio.Task[None] = asyncio.create_task(self._consume_stt())

    def _emit(self, event: EngineEvent) -> None:
        self._events.put_nowait(event)

    async def _audio_iter(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._audio_in.get()
            if frame is None:
                return
            yield frame

    async def _consume_stt(self) -> None:
        try:
            async for item in self._stt.stream(self._audio_iter()):
                if isinstance(item, PartialTranscript):
                    self._emit(item)
                elif isinstance(item, FinalTranscript):
                    self._emit(item)
                    self._messages.append({"role": "user", "content": item.text})
                    await self._run_turn()
        except EngineException as exc:
            # An STT-stream-level failure (the connection itself died, an
            # auth rejection, ...) ends this session's ability to keep
            # listening -- surfaced as an event (ADR-0006: "surfaced rather
            # than raised"), never left to crash the session's task
            # silently. Phase 2.2's fakes never raised, so this path was
            # never exercised before real adapters existed (Phase 2.3
            # deviation -- see docs/PHASE-2.3-STATUS.md section 3).
            self._emit(EngineError(code=exc.code, message=str(exc)))
        finally:
            self._events.put_nowait(None)

    async def _run_turn(self) -> None:
        self._turn_task = asyncio.create_task(self._turn())
        try:
            await self._turn_task
        except asyncio.CancelledError:
            pass
        except EngineException as exc:
            # An LLM/TTS failure inside one turn is scoped to that turn --
            # surfaced as an event and the session keeps listening for the
            # caller's next utterance, rather than tearing down STT
            # consumption for an error that has nothing to do with it.
            self._emit(EngineError(code=exc.code, message=str(exc)))
        finally:
            self._turn_task = None

    async def _turn(self) -> None:
        buffer = ""
        async for item in self._llm.stream_turn(self._messages, self.config.tools):
            if isinstance(item, str):
                buffer += item
            elif isinstance(item, ToolCallRequested):
                self._emit(item)
                # Recorded *before* the tool result, exactly as an
                # OpenAI-compatible chat-completions history requires: an
                # assistant message carrying `tool_calls`, immediately
                # followed by the matching `tool`-role message(s). Phase
                # 2.2's fakes never round-tripped a real second
                # `stream_turn()` call through a real provider, so this
                # gap was never exercised before (Phase 2.3 deviation --
                # see docs/PHASE-2.3-STATUS.md section 3). `tool_calls` is
                # a provider-neutral key any `LlmProvider` adapter may read
                # or ignore -- the contract's own message shape, not an
                # OpenAI-specific concept leaking upward.
                self._messages.append(
                    {
                        "role": "assistant",
                        "content": buffer or None,
                        "tool_calls": [
                            {"id": item.call_id, "name": item.name, "arguments": item.arguments}
                        ],
                    }
                )
                buffer = ""
                result = await self._await_tool_result(item.call_id)
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.call_id,
                        "content": result.value if result.value is not None else {},
                        "error_code": result.error_code,
                    }
                )
                await self._continue_after_tool_result()
                return
            elif isinstance(item, TurnEnded):
                break
        if buffer:
            self._messages.append({"role": "assistant", "content": buffer})
            # Emitted once, before synthesis: the durable "assistant" turn
            # (Phase 2.5) is the text the agent decided to say, independent
            # of how many AudioOut frames its synthesis produces.
            self._emit(AssistantResponse(text=buffer))
            async for audio in self._tts.synthesize(buffer, self.config.voice):
                self._emit(audio)
        self._emit(TurnEnded())

    async def _continue_after_tool_result(self) -> None:
        """A tool result re-enters the conversation as the next turn (the
        model sees the tool's output and continues), rather than this
        session assuming the turn is over -- matches `LlmProvider`'s own
        streaming shape, where a fresh `stream_turn()` call is how a new
        turn (including one continuing after a tool result) is expressed."""
        await self._turn()

    async def _await_tool_result(self, call_id: str) -> ToolResult:
        future: asyncio.Future[ToolResult] = asyncio.get_running_loop().create_future()
        self._pending_tool_results[call_id] = future
        return await future

    async def send_audio(self, frame: bytes) -> None:
        if self.closed:
            raise RuntimeError("session is closed")
        await self._audio_in.put(frame)

    async def interrupt(self) -> None:
        """Barge-in: cancel any in-flight LLM/TTS turn and drop queued agent
        audio -- idempotent, and safe with no turn in flight."""
        self.interrupts += 1
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        self._drop_pending_audio()

    def _drop_pending_audio(self) -> None:
        remaining: list[EngineEvent | None] = []
        while not self._events.empty():
            event = self._events.get_nowait()
            if isinstance(event, AudioOut):
                continue
            remaining.append(event)
        for event in remaining:
            self._events.put_nowait(event)

    async def submit_tool_result(self, result: ToolResult) -> None:
        future = self._pending_tool_results.pop(result.call_id, None)
        if future is not None and not future.done():
            future.set_result(result)
        # A result for a call_id this session no longer recognizes (already
        # answered, or the session has moved past it) is a no-op -- the same
        # "submit_tool_result() on a closed/gone session is a no-op" contract
        # `FakeEngineSession` already documents.

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        await self._audio_in.put(None)
        with contextlib.suppress(asyncio.CancelledError):
            await self._stt_task

    async def events(self) -> AsyncIterator[EngineEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event


class PipelinedEngine:
    """Starts `PipelinedEngineSession`s. Stateless apart from its three
    component providers -- exactly `ConversationEngine`'s own contract."""

    def __init__(self, stt: SttProvider, llm: LlmProvider, tts: TtsProvider) -> None:
        self._stt = stt
        self._llm = llm
        self._tts = tts

    async def start(self, config: EngineSessionConfig) -> PipelinedEngineSession:
        return PipelinedEngineSession(config, self._stt, self._llm, self._tts)
