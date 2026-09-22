"""One call's execution, start to finish (Phase 2.0 report §7, §14.1's exact
startup ordering; this phase's brief section 18):

```text
[already done by the orchestrator before this module runs, Phase 2.0 §7:
 tenant resolution, CallSession creation, Agent/AgentVersion selection,
 runtime ownership via voiceagent.runtime.assignment]
    |
load CallSession + AgentVersion snapshot  (DatabaseBoundary.run, off the loop)
    |
privacy authorization                     (DatabaseBoundary.run; DENY -> stop, engine never starts)
    |
media attachment
    |
ConversationEngine.start()
    |
audio <-> engine event pump, until cancelled or either side ends
    |
teardown: close engine, detach media, finalize CallSession,
          build CallAnalysis                (DatabaseBoundary.run)
```

**No database access happens inside the pump** (`_run_pumps()`) -- only
before it (load, authorize) and after it (finalize), each crossing
`voiceagent.runtime.db.DatabaseBoundary`. `tests/architecture
/test_runtime_db_boundary.py` asserts this module imports neither
`voiceagent.db` nor `voiceagent.tenancy.tenant_scope` directly.

**Tool Gateway arrived in Phase 2.4** (`voiceagent.tools.gateway
.ToolGateway`). `CallTaskDependencies.tool_gateway` replaces this module's
former `on_tool_call_requested` placeholder sink: a `ToolCallRequested`
event, once emitted by the engine, is now dispatched to the gateway and its
`ToolResult` is fed back with `engine_session.submit_tool_result()` -- see
`_dispatch_tool_call()` and `_run_pumps()` below. This module still never
imports the database or SaaS-OS directly: the gateway call crosses
`deps.db` internally (`voiceagent.runtime.db.DatabaseBoundary`), exactly the
same boundary this module's own `load`/`authorize`/`finalize` steps already
cross, and `_run_pumps()` itself still touches nothing but the dispatch
coroutine it was handed.

**`EngineSessionConfig.tools` is now populated from the AgentVersion's
allowlist.** `AgentVersion.config.tools` (Phase 2.0 report §9.3) names tool
*keys*; `_engine_session_config()` resolves each one that is also a real,
registered tool (`voiceagent.tools.registry.TOOL_REGISTRY`) into a
`ToolSpec(name, description, input_schema)` the model is shown. A key that is
not a registered tool is silently omitted from what the model is offered --
it is still unreachable at execution time regardless (the gateway's own
allowlist-plus-registry check is the real, independent enforcement point;
what is advertised here is a convenience, never a security boundary by
itself).

**Phase 2.8 adds one best-effort step to teardown**: after `CallSession`
finalization, `run_call_task()` calls `voiceagent.call_analysis.service
.build_call_analysis()` through the same `deps.db.run()` seam every other
write here already crosses -- no new background mechanism, no job
framework (brief section 6). It runs strictly after the audio pump has
already stopped, so it never blocks the audio path; a failure is logged and
swallowed, never allowed to crash this call's teardown or another call's
task, and is always recoverable later through the explicit
`POST /v1/call-sessions/{id}/analysis/rebuild` route.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from voiceagent.agents.models import AgentVersion
from voiceagent.agents.service import get_agent_version
from voiceagent.call_analysis.service import build_call_analysis
from voiceagent.calls.errors import InvalidCallSessionTransitionError
from voiceagent.calls.service import get_call_session, transition_call_session
from voiceagent.observability import bind_correlation_context
from voiceagent.providers.engines.contracts import (
    AssistantResponse,
    AudioOut,
    ConversationEngine,
    EngineSessionConfig,
    FinalTranscript,
    SpeechStarted,
    SystemPromptSet,
    ToolCallRequested,
    ToolResult,
    ToolSpec,
    VoiceRef,
)
from voiceagent.runtime.conversation_persistence import ConversationPersistence, PendingTurn
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.runtime.errors import DataAuthorizationDeniedError
from voiceagent.runtime.privacy import AiDataPolicySource, authorize_call_data_access
from voiceagent.telephony.contracts import CallRef, MediaProvider, TelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.tools.gateway import ToolGateway
from voiceagent.tools.registry import TOOL_REGISTRY, ToolRegistry

__all__ = [
    "CallTaskDependencies",
    "CancellationSignal",
    "ConversationPersist",
    "ToolDispatch",
    "run_call_task",
]

_logger = logging.getLogger(__name__)

#: `_run_pumps()`'s own view of tool dispatch: an async callable it awaits
#: without knowing -- or being allowed to know, per
#: `tests/architecture/test_runtime_db_boundary.py` -- anything about how the
#: result was produced. `run_call_task()` builds the real one as a closure
#: over `deps.tool_gateway.execute(...)`.
ToolDispatch = Callable[[ToolCallRequested], Awaitable[ToolResult]]

#: `_run_pumps()`'s own view of conversation persistence (Phase 2.5):
#: synchronous and non-blocking (`ConversationPersistence.enqueue()`'s own
#: contract, module docstring), so calling it can never make the pump await
#: a database write. `run_call_task()` builds the real one as a closure over
#: `deps.conversation_persistence.enqueue(call_session_id, ...)`.
ConversationPersist = Callable[[PendingTurn], None]


@dataclass
class CancellationSignal:
    """Mutated by the supervisor immediately before calling
    `asyncio.Task.cancel()` on a call task, so the task's own teardown can
    record *why* it ended (hangup vs. runtime shutdown vs. provider
    disconnect) without threading a typed payload through
    `asyncio.CancelledError` itself, which does not reliably carry one."""

    reason: str = "hangup"


@dataclass(frozen=True, slots=True)
class CallTaskDependencies:
    """Everything one call task needs, gathered in one place so
    `run_call_task()`'s own signature stays short and every dependency is
    independently substitutable in a test."""

    engine: ConversationEngine
    media: MediaProvider
    telephony: TelephonyProvider
    db: DatabaseBoundary
    policy_source: AiDataPolicySource
    tool_gateway: ToolGateway
    conversation_persistence: ConversationPersistence
    system_actor_user_id: uuid.UUID | None
    #: `voiceagent.tools.gateway`'s per-tenant service-account resolution
    #: key -- a *name*, not a fixed id (`RuntimeSettings
    #: .system_service_account_name`'s own docstring explains why a single
    #: global id cannot work across tenants).
    system_service_account_name: str
    #: Phase 2.13 (brief §15): the `voiceagent.runtime.supervisor.CallRuntime
    #: .instance_id` this call task is running on, threaded through to every
    #: `transition_call_session()` call below as `expected_runtime_instance_id`
    #: so a stale or non-owning runtime cannot progress a call it does not
    #: hold (`CallSession.runtime_instance_id`, claimed exclusively and
    #: earlier by `voiceagent.runtime.assignment.assign_call_to_runtime()`).
    #: `None` -- the default -- skips the check entirely, which is what every
    #: caller predating this phase (no Call Orchestrator exists yet to supply
    #: a real value; see `run_call_task()`'s own module docstring) continues
    #: to get.
    runtime_instance_id: str | None = None
    #: Bounded teardown (brief §4): the maximum time `run_call_task()`'s own
    #: `finally` block waits for `engine.close()`/`media.detach()`
    #: respectively before giving up on that one step and moving on to the
    #: next -- a wedged provider must delay this call's own teardown by at
    #: most this much, never indefinitely, and must never block another
    #: call's teardown at all (each call is its own `asyncio.Task`).
    engine_close_timeout_seconds: float = 5.0
    media_detach_timeout_seconds: float = 5.0


def _engine_provider_name(agent_version: AgentVersion) -> str:
    engine_config = agent_version.config.get("engine") or {}
    kind = engine_config.get("kind", "pipelined")
    component = engine_config.get(kind) or {}
    return str(component.get("provider", "fake"))


def _tool_specs(agent_version: AgentVersion, tool_registry: ToolRegistry) -> tuple[ToolSpec, ...]:
    specs: list[ToolSpec] = []
    for binding in agent_version.config.get("tools") or []:
        key = str(binding["key"])
        if key not in tool_registry:
            # Not a registered tool -- nothing correct to advertise for it.
            # Execution-time enforcement (voiceagent.tools.gateway) does not
            # depend on this list, so omitting it here is a convenience, not
            # a security decision.
            continue
        definition = tool_registry.resolve(key)
        specs.append(
            ToolSpec(
                name=definition.tool_id,
                description=definition.description,
                input_schema=definition.input_schema,
            )
        )
    return tuple(specs)


def _engine_session_config(
    agent_version: AgentVersion, tool_registry: ToolRegistry = TOOL_REGISTRY
) -> EngineSessionConfig:
    config = agent_version.config
    voice_config = config["voice"]
    return EngineSessionConfig(
        instructions=config["instructions"],
        greeting=config.get("greeting"),
        language=config.get("language", "en"),
        voice=VoiceRef(
            provider=voice_config["provider"],
            voice_id=voice_config["voice_id"],
            settings=voice_config.get("settings", {}),
        ),
        tools=_tool_specs(agent_version, tool_registry),
    )


def _final_status(reason: str) -> tuple[str, str]:
    """The *ideal* terminal status for a given cancellation reason. Not
    always reachable: `voiceagent.calls.lifecycle`'s transition table only
    allows `completed` from `answered`/`in_progress` -- a hangup that arrives
    before the call was ever answered (a real, exercised case: cancellation
    can land at any point, including mid-startup, see `run_call_task()`'s
    own docstring) cannot legally become `completed`. `run_call_task()`'s
    finally block falls back to `interrupted` when this target is rejected,
    rather than leaving the row stuck non-terminal."""
    if reason == "hangup":
        return "completed", "completed"
    if reason == "runtime_shutdown":
        return "interrupted", "runtime_shutdown"
    if reason in ("provider_disconnect", "media_disconnect"):
        return "failed", reason
    return "interrupted", reason


async def _run_pumps(
    call_session_id: uuid.UUID,
    media_stream,
    engine_session,
    dispatch_tool_call: ToolDispatch,
    persist_turn: ConversationPersist | None = None,
) -> None:
    """The two concurrent, audio-adjacent loops -- no database access, no
    tenant context, no SaaS-OS import anywhere in this function (or in
    `_execute_and_submit_tool_call()` below, which is nested inside it for
    exactly that reason: `dispatch_tool_call` is the only way this function
    or its helpers ever reach the Tool Gateway, and the gateway's own DB work
    happens on the far side of that one already-awaited coroutine, never
    referenced here by name -- `tests/architecture/test_runtime_db_boundary
    .py::test_the_audio_pump_never_references_a_sync_db_touching_name`).

    **`persist_turn` (Phase 2.5) is the identical shape of seam**:
    synchronous and non-blocking (`ConversationPersist`'s own contract), so
    calling it here can never make this function await a database write --
    the actual write happens on the far side of a bounded queue this
    function never sees, in `voiceagent.runtime.conversation_persistence`.
    `None` is accepted (and every call defaults to it) so a caller that has
    no persistence configured -- every hermetic test in
    `tests/runtime/test_call_task_tools.py` predating Phase 2.5 -- keeps
    working unchanged."""

    tool_tasks: set[asyncio.Task[None]] = set()

    def _persist(turn: PendingTurn) -> None:
        if persist_turn is not None:
            persist_turn(turn)

    async def pump_caller_audio() -> None:
        async for frame in media_stream.receive():
            await engine_session.send_audio(frame)

    async def _execute_and_submit_tool_call(request: ToolCallRequested) -> None:
        try:
            result = await dispatch_tool_call(request)
        except Exception:  # noqa: BLE001 -- a Tool Gateway/config bug must
            # not hang the model waiting for a result forever, and must not
            # crash this call's own event pump; the model instead gets one
            # normalized failure, and the operator gets a log line.
            _logger.exception(
                "tool dispatch for CallSession %s, call_id=%r raised unexpectedly",
                call_session_id,
                request.call_id,
            )
            result = ToolResult(
                call_id=request.call_id, error_code="internal_error", retryable=False
            )
        # The matching "tool_result" turn, persisted only once the result is
        # actually known -- always after this same call_id's "tool_call"
        # turn was already enqueued below, in pump_engine_events(), never
        # before (brief section 5's ordering requirement).
        _persist(
            PendingTurn(
                event_id=result.call_id,
                role="tool_result",
                tool_payload={"value": result.value, "error_code": result.error_code},
            )
        )
        with contextlib.suppress(Exception):
            # A closed/gone engine session's submit_tool_result() is
            # documented as a no-op (voiceagent.providers.engines.pipelined),
            # but a fake or future engine is not guaranteed to be that
            # forgiving -- this call's own teardown must never fail because a
            # tool result arrived after the session already closed.
            await engine_session.submit_tool_result(result)

    async def pump_engine_events() -> None:
        async for event in engine_session.events():
            if isinstance(event, AudioOut):
                await media_stream.send(event.frame)
            elif isinstance(event, ToolCallRequested):
                _persist(
                    PendingTurn(
                        event_id=event.call_id,
                        role="tool_call",
                        tool_payload={"name": event.name, "arguments": dict(event.arguments)},
                    )
                )
                task = asyncio.create_task(_execute_and_submit_tool_call(event))
                tool_tasks.add(task)
                task.add_done_callback(tool_tasks.discard)
            elif isinstance(event, SystemPromptSet):
                _persist(
                    PendingTurn(event_id=event.event_id, role="system", content=event.instructions)
                )
            elif isinstance(event, FinalTranscript):
                _persist(PendingTurn(event_id=event.event_id, role="user", content=event.text))
            elif isinstance(event, AssistantResponse):
                _persist(PendingTurn(event_id=event.event_id, role="assistant", content=event.text))
            elif isinstance(event, SpeechStarted):
                # Barge-in (Phase 2.13, brief §9): the Call Runtime is the
                # one place that calls `interrupt()` -- uniformly, across
                # every `ConversationEngine` implementation
                # (`voiceagent.providers.engines.realtime.RealtimeProviderSession
                # .interrupt()`'s own docstring). Unconditional and
                # unguarded by whether a turn is actually in flight:
                # `interrupt()` is documented as idempotent and safe to call
                # at any point, including with nothing to interrupt.
                await engine_session.interrupt()
            # PartialTranscript/SpeechEnded/TurnEnded/UsageReported/
            # EngineError: observability-only -- never persisted (brief
            # section 4: "do not persist every transient STT partial").

    caller_task = asyncio.create_task(pump_caller_audio())
    events_task = asyncio.create_task(pump_engine_events())
    try:
        await asyncio.gather(caller_task, events_task)
    finally:
        pending = (caller_task, events_task, *tool_tasks)
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def run_call_task(
    *,
    context: TenantContext,
    call_session_id: uuid.UUID,
    call_ref: CallRef,
    deps: CallTaskDependencies,
    cancellation: CancellationSignal,
) -> None:
    """Run one call from load through teardown. Raises whatever a genuine
    failure raises (the caller -- `voiceagent.runtime.supervisor` -- is the
    error boundary that keeps one call's failure from affecting another's,
    per this phase's brief section 27); a clean hangup or an authorization
    denial both return normally.

    **Cancellation may arrive at any point** -- including before media is
    attached or the engine has started, e.g. under load, while still
    awaiting the very first `DatabaseBoundary.run()` call (this is not
    hypothetical: it is exactly what a runtime supervising many concurrent
    calls against a small thread pool produces under real concurrency, and
    is exercised directly by `tests/integration/test_runtime_integration.py
    ::test_ten_simultaneous_calls`). The entire body below is therefore one
    `try`/`finally`: whatever has been created by the time cancellation
    lands (`media_stream`, `engine_session`, neither, or both) is torn down,
    and the `CallSession` is always finalized -- never left in a
    non-terminal status with no runtime left alive to ever finish it
    (`call_sessions.runtime_instance_id` was already claimed by *this*
    runtime before `run_call_task()` was ever invoked, so a call abandoned
    mid-startup would otherwise never be picked up by reconciliation either,
    since that mechanism only reacts to a runtime whose heartbeat has
    actually expired -- ADR-0008 point 9)."""
    with bind_correlation_context(
        tenant_id=str(context.tenant_id), request_id=str(call_session_id)
    ):
        media_stream = None
        engine_session = None
        finalized = False
        try:
            call = await deps.db.run(get_call_session, context, call_session_id)
            agent_version = await deps.db.run(get_agent_version, context, call.agent_version_id)

            try:
                await deps.db.run(
                    authorize_call_data_access,
                    tenant_id=context.tenant_id,
                    call_session_id=call_session_id,
                    data_classification=agent_version.config["privacy"]["data_classification"],
                    purpose=agent_version.config["privacy"]["purpose"],
                    provider=_engine_provider_name(agent_version),
                    policy_source=deps.policy_source,
                    system_actor_user_id=deps.system_actor_user_id,
                )
            except DataAuthorizationDeniedError:
                await deps.db.run(
                    transition_call_session,
                    context,
                    call_session_id,
                    to_status="failed",
                    end_reason="authorization_denied",
                    expected_runtime_instance_id=deps.runtime_instance_id,
                )
                finalized = True
                return

            media_stream = await deps.media.attach(call_ref)
            engine_session = await deps.engine.start(_engine_session_config(agent_version))
            deps.conversation_persistence.start(context, call_session_id)

            async def _dispatch_tool_call(request: ToolCallRequested) -> ToolResult:
                return await deps.tool_gateway.execute(
                    db=deps.db,
                    context=context,
                    call_session_id=call_session_id,
                    agent_version=agent_version,
                    call_ref=call_ref,
                    telephony=deps.telephony,
                    system_service_account_name=deps.system_service_account_name,
                    request=request,
                )

            def _persist_turn(turn: PendingTurn) -> None:
                deps.conversation_persistence.enqueue(call_session_id, turn)

            # initiated -> answered -> in_progress
            # (voiceagent.calls.lifecycle's own transition table has no
            # initiated -> in_progress edge; media attachment/engine start
            # is this phase's stand-in for "answered", since no real
            # FreeSWITCH ANSWERED event is wired up yet).
            await deps.db.run(
                transition_call_session,
                context,
                call_session_id,
                to_status="answered",
                expected_runtime_instance_id=deps.runtime_instance_id,
            )
            await deps.db.run(
                transition_call_session,
                context,
                call_session_id,
                to_status="in_progress",
                expected_runtime_instance_id=deps.runtime_instance_id,
            )

            await _run_pumps(
                call_session_id, media_stream, engine_session, _dispatch_tool_call, _persist_turn
            )
        finally:
            deps.tool_gateway.forget_call(call_session_id)
            await deps.conversation_persistence.finish(call_session_id)
            if engine_session is not None:
                # Bounded (brief §4): a wedged engine/provider must delay
                # this call's own teardown by at most
                # `engine_close_timeout_seconds`, never indefinitely --
                # `PipelinedEngineSession.close()` is already internally
                # bounded the same way; this is the second, independent
                # bound at the one place every engine's close() is awaited,
                # regardless of which engine it is. One cleanup step's
                # failure/timeout must never prevent the next
                # (media.detach(), finalization) from running.
                try:
                    await asyncio.wait_for(
                        engine_session.close(), timeout=deps.engine_close_timeout_seconds
                    )
                except TimeoutError:
                    _logger.warning(
                        "engine_session.close() timed out for CallSession %s (%.1fs)",
                        call_session_id,
                        deps.engine_close_timeout_seconds,
                    )
                except Exception:  # noqa: BLE001 -- best-effort external cleanup; see above.
                    _logger.exception(
                        "engine_session.close() raised for CallSession %s", call_session_id
                    )
            if media_stream is not None:
                try:
                    await asyncio.wait_for(
                        deps.media.detach(call_ref), timeout=deps.media_detach_timeout_seconds
                    )
                except TimeoutError:
                    _logger.warning(
                        "media.detach() timed out for CallSession %s (%.1fs)",
                        call_session_id,
                        deps.media_detach_timeout_seconds,
                    )
                except Exception:  # noqa: BLE001 -- best-effort external cleanup; see above.
                    _logger.exception("media.detach() raised for CallSession %s", call_session_id)
            if not finalized:
                status, end_reason = _final_status(cancellation.reason)
                try:
                    # No `expected_runtime_instance_id` here, deliberately:
                    # finalization's one job is to always leave the row in a
                    # terminal state (this function's own docstring, "never
                    # left in a non-terminal status") -- that guarantee must
                    # hold even in the narrow, adversarial case an ownership
                    # check exists to catch, so it is enforced on the
                    # *operative* transitions above, never on this one.
                    await deps.db.run(
                        transition_call_session,
                        context,
                        call_session_id,
                        to_status=status,
                        end_reason=end_reason,
                    )
                except InvalidCallSessionTransitionError:
                    # `status` (typically "completed") is not reachable from
                    # wherever the call actually got to (e.g. hangup arrived
                    # before it was ever answered) -- "interrupted" is
                    # reachable from every non-terminal status, so fall back
                    # to it rather than leaving the row stuck non-terminal.
                    with contextlib.suppress(Exception):
                        await deps.db.run(
                            transition_call_session,
                            context,
                            call_session_id,
                            to_status="interrupted",
                            end_reason=end_reason,
                        )
                except Exception:  # noqa: BLE001 -- Phase 2.0 report §17's own
                    # "DB unavailable" row: a failed finalization write must
                    # not crash the call task or another call; reconciliation
                    # is the documented recovery path once this runtime's own
                    # heartbeat later expires, if it ever does. Logged, not
                    # silently dropped, so the failure is at least observable.
                    _logger.exception(
                        "failed to finalize CallSession %s to status=%r", call_session_id, status
                    )
            # Phase 2.8: best-effort post-call analysis build, after the
            # CallSession's own finalization above (accurate duration_ms
            # needs ended_at already set) -- the exact same DatabaseBoundary
            # seam every other write in this function already crosses, never
            # a new background/job mechanism (brief section 6). Never
            # allowed to fail call teardown or crash another call's task:
            # a stale/missing CallAnalysis row is always recoverable later
            # through `POST /v1/call-sessions/{id}/analysis/rebuild`, so
            # logging and moving on is correct here, not merely convenient.
            try:
                await deps.db.run(build_call_analysis, context, call_session_id)
            except Exception:  # noqa: BLE001 -- see comment above.
                _logger.exception(
                    "failed to build CallAnalysis for CallSession %s", call_session_id
                )
