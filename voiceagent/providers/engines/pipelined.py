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

**Phase 2.13 hardening added five things, all backward compatible (every
new constructor parameter defaults to the Phase 2.2-2.5 behavior):**

1. **Bounded queues** (`audio_queue_maxsize`/`event_queue_maxsize`,
   §6 "media must not cause unbounded memory growth"). `_audio_in` and
   `_events` were plain, unbounded `asyncio.Queue()`s -- a slow STT/media
   consumer could grow either without limit. Both are now bounded;
   `send_audio()`'s `await self._audio_in.put(frame)` naturally backpressures
   the caller-audio pump (`voiceagent.runtime.call_task._run_pumps`) when
   full, exactly the "slow consumer" behavior §6 asks for -- this blocks
   only *this call's own* task, never the shared event loop or another
   call, since every call already runs as its own `asyncio.Task`
   (`voiceagent.runtime.supervisor.CallRuntime`). A terminal sentinel
   (`None`) must never be lost to a full queue, so it is always delivered
   through `_force_put_nowait()` below, which evicts the oldest entry
   rather than blocking or raising.
2. **A closed-session guard in `_consume_stt()`** (§7 "STT result arrives
   after call termination"): a `FinalTranscript` that surfaces after
   `close()` has already run (a real STT stream can take a moment to unwind
   after its `CloseStream` control frame) is dropped rather than starting a
   new turn on an already-closed session.
3. **Barge-in signal emission** (§9): `SpeechStarted`/`SpeechEnded` are part
   of `EngineEvent` (`voiceagent.providers.engines.contracts`) and
   `RealtimeProviderSession.interrupt()`'s own docstring already says "the
   Call Runtime calls `interrupt()` uniformly across every engine type" --
   but nothing ever emitted `SpeechStarted` for a pipelined session, so nothing
   ever called `interrupt()` for one. A caller's first `PartialTranscript` of
   an utterance is this pipeline's own speech-activity signal (the STT
   provider is already listening continuously); `_consume_stt()` now emits
   `SpeechStarted`/`SpeechEnded` around it, and
   `voiceagent.runtime.call_task._run_pumps()` calls `interrupt()` when it
   sees one -- unconditionally and unconcerned with whether a turn is
   actually in flight, exactly as `interrupt()`'s own contract already
   promises ("idempotent, and safe to call at any point in a turn").
4. **Idle timeouts on every provider stream** (§10 "every external provider
   operation on the live call path must have an explicit timeout"):
   `stt_idle_timeout_seconds`/`llm_idle_timeout_seconds`/
   `tts_idle_timeout_seconds`, each independently configurable (never one
   collapsed global timeout, per §10's own instruction), bound how long this
   session waits for the *next* item from a provider's async stream. A
   provider that stops producing without raising or closing (a wedged
   websocket, a stalled SSE response) surfaces as one `EngineException`
   (`EngineErrorCode.TRANSIENT`), handled exactly like any other provider
   failure already is -- never a silent, unbounded hang.
5. **`_run_turn()` no longer swallows a cancellation meant for `_stt_task`
   itself** (§5 "no fire-and-forget task may silently outlive its call"):
   its `except asyncio.CancelledError` is meant to absorb a *directly*
   cancelled turn (`interrupt()`/`close()` cancelling `_turn_task` on its
   own) so STT consumption keeps running -- but the same coroutine also
   runs inside `_stt_task`, which `close()`'s own bounded wait (point 4
   above) can cancel directly on timeout. `Task.cancelling()` (3.11+) tells
   the two apart; only the latter is re-raised.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from voiceagent.providers.engines.contracts import (
    AssistantResponse,
    AudioOut,
    EngineError,
    EngineErrorCode,
    EngineEvent,
    EngineException,
    EngineSessionConfig,
    FinalTranscript,
    LlmProvider,
    PartialTranscript,
    SpeechEnded,
    SpeechStarted,
    SttProvider,
    SystemPromptSet,
    ToolCallRequested,
    ToolResult,
    TtsProvider,
    TurnEnded,
)

__all__ = ["PipelinedEngine", "PipelinedEngineSession"]

_logger = logging.getLogger(__name__)

#: Defaults preserve exact prior behavior for every existing caller
#: (`PipelinedEngine(stt, llm, tts)` with no other arguments) -- generous
#: enough that no hermetic fake (instantaneous, or `chunk_delay`-paced in the
#: tens-of-milliseconds range) ever trips them, small enough that a genuinely
#: wedged provider is caught well within one call's lifetime.
_DEFAULT_AUDIO_QUEUE_MAXSIZE = 200
_DEFAULT_EVENT_QUEUE_MAXSIZE = 500
_DEFAULT_STT_IDLE_TIMEOUT_SECONDS = 20.0
_DEFAULT_LLM_IDLE_TIMEOUT_SECONDS = 20.0
_DEFAULT_TTS_IDLE_TIMEOUT_SECONDS = 20.0
_DEFAULT_CLOSE_TIMEOUT_SECONDS = 5.0


def _force_put_nowait[T](queue: asyncio.Queue[T], item: T) -> None:
    """Put `item` without ever blocking or raising `QueueFull` -- used only
    for terminal sentinels (`None`), which must never be lost to a queue a
    slow consumer has filled with real events. Evicts the oldest queued item
    to make room, exactly once; a sentinel is dropped-behind, never the
    reverse, since nothing is ever read again after it (`events()`/
    `_audio_iter()` both return the moment they see one)."""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        queue.put_nowait(item)


async def _bounded[T](
    aiter: AsyncIterator[T], timeout_seconds: float, provider_label: str
) -> AsyncIterator[T]:
    """Wrap any provider's async stream so that waiting for its *next* item
    is bounded (§10) -- a provider that stops producing without raising or
    ending its stream surfaces as one normalized `EngineException` instead of
    hanging this session's turn/STT-consumption task forever."""
    while True:
        try:
            item = await asyncio.wait_for(anext(aiter), timeout=timeout_seconds)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            raise EngineException(
                EngineErrorCode.TRANSIENT,
                f"{provider_label}: no activity within {timeout_seconds}s",
            ) from exc
        yield item


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
        *,
        audio_queue_maxsize: int = _DEFAULT_AUDIO_QUEUE_MAXSIZE,
        event_queue_maxsize: int = _DEFAULT_EVENT_QUEUE_MAXSIZE,
        stt_idle_timeout_seconds: float = _DEFAULT_STT_IDLE_TIMEOUT_SECONDS,
        llm_idle_timeout_seconds: float = _DEFAULT_LLM_IDLE_TIMEOUT_SECONDS,
        tts_idle_timeout_seconds: float = _DEFAULT_TTS_IDLE_TIMEOUT_SECONDS,
        close_timeout_seconds: float = _DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config
        self.interrupts = 0
        self.closed = False
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._stt_idle_timeout_seconds = stt_idle_timeout_seconds
        self._llm_idle_timeout_seconds = llm_idle_timeout_seconds
        self._tts_idle_timeout_seconds = tts_idle_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._messages: list[dict[str, object]] = _initial_messages(config)
        self._audio_in: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=audio_queue_maxsize)
        self._events: asyncio.Queue[EngineEvent | None] = asyncio.Queue(maxsize=event_queue_maxsize)
        self._pending_tool_results: dict[str, asyncio.Future[ToolResult]] = {}
        self._turn_task: asyncio.Task[None] | None = None
        self._caller_speaking = False
        # Durable "system"/"assistant" turns (Phase 2.5): emitted once, up
        # front, before any audio has been sent or received -- the queue is
        # freshly constructed and empty, so a non-blocking put can never
        # fail here even though `_events` is now bounded (Phase 2.13).
        _force_put_nowait(self._events, SystemPromptSet(instructions=config.instructions))
        if config.greeting:
            _force_put_nowait(self._events, AssistantResponse(text=config.greeting))
        self._stt_task: asyncio.Task[None] = asyncio.create_task(self._consume_stt())

    async def _emit(self, event: EngineEvent) -> None:
        """Bounded and backpressuring (Phase 2.13, §6): blocks the calling
        task -- never the shared event loop, never another call -- until
        room exists, rather than growing `_events` without limit.

        Once the session is closed, nothing is guaranteed to still be
        draining `events()` (`voiceagent.runtime.call_task._run_pumps()`'s
        own consumer task has typically already been cancelled by the time
        `close()` runs, in its caller's teardown) -- blocking on `put()`
        here could then wait for room that never comes, which would in turn
        make `close()`'s own bounded wait for `_stt_task` (§4) less bounded
        than it looks. A post-close event is therefore delivered
        best-effort instead, through the same non-blocking, sentinel-safe
        path terminal `None`s already use (`_force_put_nowait()`) -- this
        keeps a genuine late diagnostic (an `EngineError` for a failure that
        only surfaced while unwinding, exactly as
        `tests/providers/test_pipelined_engine_error_handling.py
        ::test_stt_failure_surfaces_as_engine_error_not_a_crash` exercises)
        visible to a caller that still drains `events()` after `close()`
        returns, without ever risking a deadlock."""
        if self.closed:
            _force_put_nowait(self._events, event)
            return
        await self._events.put(event)

    async def _audio_iter(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._audio_in.get()
            if frame is None:
                return
            yield frame

    async def _consume_stt(self) -> None:
        try:
            async for item in _bounded(
                self._stt.stream(self._audio_iter()), self._stt_idle_timeout_seconds, "stt"
            ):
                if isinstance(item, PartialTranscript):
                    if not self._caller_speaking:
                        self._caller_speaking = True
                        # Barge-in signal (§9): the runtime
                        # (`voiceagent.runtime.call_task._run_pumps()`) calls
                        # `interrupt()` when it sees this -- unconditionally
                        # safe even with no turn in flight, per
                        # `EngineSession.interrupt()`'s own contract.
                        await self._emit(SpeechStarted())
                    await self._emit(item)
                elif isinstance(item, FinalTranscript):
                    if self._caller_speaking:
                        self._caller_speaking = False
                        await self._emit(SpeechEnded())
                    if self.closed:
                        # A late result surfacing after close() already ran
                        # (§7: "STT result arrives after call termination")
                        # -- this session has nothing left to feed it to.
                        continue
                    await self._emit(item)
                    self._messages.append({"role": "user", "content": item.text})
                    await self._run_turn()
        except EngineException as exc:
            # An STT-stream-level failure (the connection itself died, an
            # auth rejection, a stall past `stt_idle_timeout_seconds`, ...)
            # ends this session's ability to keep listening -- surfaced as
            # an event (ADR-0006: "surfaced rather than raised"), never left
            # to crash the session's task silently. Phase 2.2's fakes never
            # raised, so this path was never exercised before real adapters
            # existed (Phase 2.3 deviation -- see docs/PHASE-2.3-STATUS.md
            # section 3). Emitted unconditionally, even if `close()` has
            # already run -- `_emit()`'s own post-close path makes this
            # safe; see its docstring.
            await self._emit(EngineError(code=exc.code, message=str(exc)))
        finally:
            # A terminal sentinel must never be lost to a full queue -- see
            # `_force_put_nowait()`'s own docstring.
            _force_put_nowait(self._events, None)

    async def _run_turn(self) -> None:
        self._turn_task = asyncio.create_task(self._turn())
        try:
            await self._turn_task
        except asyncio.CancelledError:
            # A *directly* cancelled turn (`interrupt()`/`close()` calling
            # `self._turn_task.cancel()` on its own) is meant to be absorbed
            # right here -- STT consumption keeps running, exactly the
            # barge-in/close() contract. But this coroutine runs inside
            # `_stt_task` (`_consume_stt()`'s own `await self._run_turn()`),
            # and `_stt_task` can *itself* be the one being cancelled (§5/§9:
            # `close()`'s bounded `asyncio.wait_for(self._stt_task, ...)`
            # cancels it directly on timeout; so does process/event-loop
            # shutdown cancelling every remaining task). Swallowing that
            # cancellation here, indistinguishably from an interrupted turn,
            # would let `_consume_stt()`'s loop resume and potentially block
            # forever on the next provider read -- defeating the very
            # cancellation this was raised for. `Task.cancelling()` (3.11+)
            # tells them apart: it counts cancellation requests made against
            # *this* task specifically, never one made against `_turn_task`
            # alone, so it is nonzero only when `_stt_task` itself must stop.
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling() > 0:
                raise
        except EngineException as exc:
            # An LLM/TTS failure inside one turn is scoped to that turn --
            # surfaced as an event and the session keeps listening for the
            # caller's next utterance, rather than tearing down STT
            # consumption for an error that has nothing to do with it.
            await self._emit(EngineError(code=exc.code, message=str(exc)))
        finally:
            self._turn_task = None

    async def _turn(self) -> None:
        buffer = ""
        async for item in _bounded(
            self._llm.stream_turn(self._messages, self.config.tools),
            self._llm_idle_timeout_seconds,
            "llm",
        ):
            if isinstance(item, str):
                buffer += item
            elif isinstance(item, ToolCallRequested):
                await self._emit(item)
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
            await self._emit(AssistantResponse(text=buffer))
            async for audio in _bounded(
                self._tts.synthesize(buffer, self.config.voice),
                self._tts_idle_timeout_seconds,
                "tts",
            ):
                await self._emit(audio)
        await self._emit(TurnEnded())

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
        """Idempotent, and bounded (§4 "every external cleanup operation
        must have a bounded timeout"): the sentinel handoff to `_audio_in`
        never blocks (`_force_put_nowait()`, needed now that the queue is
        bounded -- a stalled downstream consumer must never stop a call from
        closing), and waiting for `_stt_task` to unwind is capped at
        `close_timeout_seconds`, mirroring the exact bounded-drain pattern
        `voiceagent.runtime.conversation_persistence._CallWorker.shutdown()`
        already establishes for this codebase. A provider stream stuck past
        `stt_idle_timeout_seconds` is already caught by `_bounded()` well
        before this timeout would ever fire; this is the second, explicit
        bound that makes `close()` deterministic on its own, independent of
        that chain."""
        if self.closed:
            return
        self.closed = True
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        _force_put_nowait(self._audio_in, None)
        try:
            await asyncio.wait_for(self._stt_task, timeout=self._close_timeout_seconds)
        except TimeoutError:
            # `wait_for()` has already cancelled `_stt_task` and awaited that
            # cancellation by the time it raises this -- nothing further to
            # tear down here, only worth an operator-visible log line.
            _logger.warning(
                "PipelinedEngineSession.close(): stt task did not stop within %.1fs",
                self._close_timeout_seconds,
            )
        except asyncio.CancelledError:
            pass

    async def events(self) -> AsyncIterator[EngineEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event


class PipelinedEngine:
    """Starts `PipelinedEngineSession`s. Stateless apart from its three
    component providers -- exactly `ConversationEngine`'s own contract.

    The keyword-only hardening parameters (Phase 2.13) are forwarded
    verbatim to every session this engine starts; each defaults to
    `PipelinedEngineSession`'s own default, so `PipelinedEngine(stt, llm,
    tts)` -- every call site predating this phase -- is unaffected."""

    def __init__(
        self,
        stt: SttProvider,
        llm: LlmProvider,
        tts: TtsProvider,
        *,
        audio_queue_maxsize: int = _DEFAULT_AUDIO_QUEUE_MAXSIZE,
        event_queue_maxsize: int = _DEFAULT_EVENT_QUEUE_MAXSIZE,
        stt_idle_timeout_seconds: float = _DEFAULT_STT_IDLE_TIMEOUT_SECONDS,
        llm_idle_timeout_seconds: float = _DEFAULT_LLM_IDLE_TIMEOUT_SECONDS,
        tts_idle_timeout_seconds: float = _DEFAULT_TTS_IDLE_TIMEOUT_SECONDS,
        close_timeout_seconds: float = _DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._audio_queue_maxsize = audio_queue_maxsize
        self._event_queue_maxsize = event_queue_maxsize
        self._stt_idle_timeout_seconds = stt_idle_timeout_seconds
        self._llm_idle_timeout_seconds = llm_idle_timeout_seconds
        self._tts_idle_timeout_seconds = tts_idle_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds

    async def start(self, config: EngineSessionConfig) -> PipelinedEngineSession:
        return PipelinedEngineSession(
            config,
            self._stt,
            self._llm,
            self._tts,
            audio_queue_maxsize=self._audio_queue_maxsize,
            event_queue_maxsize=self._event_queue_maxsize,
            stt_idle_timeout_seconds=self._stt_idle_timeout_seconds,
            llm_idle_timeout_seconds=self._llm_idle_timeout_seconds,
            tts_idle_timeout_seconds=self._tts_idle_timeout_seconds,
            close_timeout_seconds=self._close_timeout_seconds,
        )
