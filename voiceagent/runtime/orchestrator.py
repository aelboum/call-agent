"""The Call Orchestrator (Phase 2.22).

The missing application-level bridge Phase 2.21 deliberately left unbuilt
(`scripts/run_call_runtime.py`'s own module docstring; `voiceagent/runtime
/errors.py`'s own comment naming it explicitly). This module coordinates
existing components -- it does not replace any of them:

```text
FreeSWITCH OFFERED event
    -> voiceagent.runtime.telephony_events.TelephonyEventRouter (Phase 2.21,
       unmodified apart from one additive callback)
    -> CallOrchestrator._handle_offer()               (this module)
        -> voiceagent.calls.routing.resolve_inbound_route()      (authoritative
           tenant/agent resolution -- Phase 2.22's own new primitive)
        -> voiceagent.agents.service.{get_agent,select_agent_version_id,
           get_agent_version}                          (unmodified, already existed)
        -> voiceagent.calls.service.create_call_session()        (idempotent via
           fs_channel_uuid, Phase 2.22 addition to an existing function)
        -> voiceagent.runtime.privacy.authorize_call_data_access() (unmodified)
        -> voiceagent.runtime.assignment.assign_call_to_runtime()  (unmodified --
           already self-documented as "Call Orchestrator code")
        -> voiceagent.telephony.freeswitch.provider.FreeSwitchTelephonyProvider
           .answer()/.start_media_stream()             (Phase 2.21, first caller)
        -> voiceagent.runtime.supervisor.CallRuntime.start_call()  (unmodified)
            -> voiceagent.runtime.call_task.run_call_task()        (unmodified)
```

**Where this runs**: inside every `call-runtime` process, one
`CallOrchestrator` per process, each reacting independently to the identical
broadcast FreeSWITCH event stream its own `ManagedEslConnection` (Phase
2.21) receives -- there is no separate orchestrator process and no new
cross-process message bus. Correctness under a multi-process fleet does not
depend on only one process seeing an event: it falls out of the *database's*
own guarantees, already built by prior phases, applied to a race every
process's orchestrator runs unmodified:

* `voiceagent.calls.service.create_call_session()`'s partial unique index on
  `fs_channel_uuid` (migrations/0012) -- every process's `_handle_offer()`
  converges on the *same* `CallSession` row for the same external call, no
  matter how many of them raced to create it first.
* `voiceagent.calls.service.claim_runtime_ownership()`'s `SELECT ... FOR
  UPDATE`-backed exclusivity (pre-existing, ADR-0008 point 10) -- exactly
  one runtime instance ends up as `CallSession.runtime_instance_id`, no
  matter how many processes' `assign_call_to_runtime()` calls raced.
* `voiceagent.calls.service.claim_call_for_activation()`'s `SELECT ... FOR
  UPDATE`-backed `initiated -> ringing` claim (Phase 2.22's own addition,
  taken *after* the ownership check above, never before it -- see its own
  docstring for why the ordering matters) -- the one race ownership alone
  does not close: two truly concurrent duplicate events *this process*
  both independently confirms itself the owner of (same-instance reclaim is
  itself idempotent-success) must still not both call `answer()`.
* `voiceagent.runtime.supervisor.CallRuntime.start_call()`'s own existing
  in-process idempotency (`is_running()`) -- a final, fourth, independent
  backstop even within the one process that *does* own the call.

A process whose own `CallRuntime.instance_id` did not win the ownership
claim does nothing further for that call -- see `_handle_offer()`'s own
`if winning_instance_id != self._call_runtime.instance_id: return`. This is
by design, not a gap: the *winning* process's own orchestrator independently
processes the identical broadcast event and reaches the same conclusion
about itself.

**Authorization ordering is never shortcut**: `authorize_call_data_access()`
is called, and must succeed, strictly before this module ever calls
`answer()`/`start_media_stream()`/`CallRuntime.start_call()` -- an
unauthorized call is hung up, never answered, never media-activated, never
given to `ConversationEngine`. See `_handle_offer()`'s own linear structure;
there is no path through this function that reaches media/runtime code
without having passed the authorization call first.

**Telephony/AI boundaries preserved**: this module imports no ESL wire-frame
type, no FreeSWITCH event object, and no `mod_audio_stream` framing detail
-- only `voiceagent.telephony.contracts` (`CallEvent`, `HangupCause`,
`TransportError`) and the two `FreeSwitchTelephonyProvider`-specific methods
already added, by name, as a `TelephonyProvider`-shaped object's own extra
attributes (`answer`/`hangup`/`start_media_stream`, called through the
*contract* those first two are part of; `start_media_stream` is a documented
FreeSWITCH-specific extra, called explicitly, never through the shared
`TelephonyProvider` Protocol type). It builds a `ConversationEngine` via
`voiceagent.providers.engines.factory.build_conversation_engine()`, driven
entirely by `AgentVersion.config` -- no provider name, model name, or prompt
is hard-coded anywhere in this file.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Literal, Protocol, runtime_checkable

from voiceagent.agents.config import EngineSelection
from voiceagent.agents.errors import AgentNotFoundError, AgentVersionNotFoundError
from voiceagent.agents.models import Agent, AgentVersion
from voiceagent.agents.service import get_agent, get_agent_version, select_agent_version_id
from voiceagent.calls.errors import CallSessionAlreadyOwnedError
from voiceagent.calls.models import CallSession
from voiceagent.calls.routing import resolve_inbound_route
from voiceagent.calls.service import (
    claim_call_for_activation,
    create_call_session,
    transition_call_session,
)
from voiceagent.metrics import record_orchestration_event
from voiceagent.providers.engines.factory import build_conversation_engine
from voiceagent.runtime.assignment import assign_call_to_runtime
from voiceagent.runtime.call_task import CallTaskDependencies
from voiceagent.runtime.conversation_persistence import ConversationPersistence
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.runtime.errors import DataAuthorizationDeniedError, NoRuntimeCapacityError
from voiceagent.runtime.heartbeat import HeartbeatStore
from voiceagent.runtime.privacy import AiDataPolicySource, authorize_call_data_access
from voiceagent.runtime.supervisor import CallRuntime
from voiceagent.runtime.telephony_events import TelephonyEventRouter
from voiceagent.telephony.contracts import (
    CallEvent,
    CallEventType,
    CallRef,
    HangupCause,
    MediaProvider,
    TelephonyError,
    TelephonyProvider,
    TransportError,
)
from voiceagent.tenancy import TenantContext
from voiceagent.tools.gateway import ToolGateway

__all__ = ["CallOrchestrator"]

_logger = logging.getLogger(__name__)


@runtime_checkable
class _MediaStreamCapableTelephonyProvider(TelephonyProvider, Protocol):
    """`TelephonyProvider` plus exactly the one FreeSWITCH-specific extra
    this orchestrator needs to call by name (`voiceagent.telephony.freeswitch
    .provider.FreeSwitchTelephonyProvider.start_media_stream()`, added in
    Phase 2.21, given its first real caller here). A structural `Protocol`,
    not an import of anything FreeSWITCH-specific: this module never imports
    `voiceagent.telephony.freeswitch.provider` or any other concrete
    adapter, preserving the telephony boundary (brief section 18) while
    still letting this one, already-designed-to-be-vendor-specific method be
    called through a typed name rather than `getattr`/`cast`. Takes only
    `call_ref` -- the concrete adapter mints and owns its own media ticket
    (Phase 2.22 correction to `provider.py` itself, so this module never
    needs to import `voiceagent.telephony.freeswitch.media_transport`
    either)."""

    async def start_media_stream(self, call_ref: CallRef) -> None: ...


#: Mirrors `voiceagent.metrics.record_orchestration_event()`'s own Literal
#: exactly -- kept as one local alias so every `_reject()`/inline call site
#: in this module is checked against the identical bounded vocabulary.
_Outcome = Literal[
    "started",
    "unknown_route",
    "route_disabled",
    "unknown_agent",
    "agent_version_unavailable",
    "duplicate_ignored",
    "authorization_denied",
    "no_capacity",
    "ownership_lost",
    "media_unavailable",
    "call_started",
    "error",
]


def _build_system_context(tenant_id: uuid.UUID) -> TenantContext:
    """`actor_id`/`membership_id` carry no foreign key anywhere this
    orchestrator's own codepath reaches -- identical reasoning to
    `voiceagent.followups.worker.FollowUpWorker`/`voiceagent.call_intelligence
    .worker.CallAiAnalysisWorker`'s own `_build_context()`, the two existing
    precedents for a server-triggered (non-HTTP) `TenantContext`. The
    *privacy* check's own acting principal is `system_actor_user_id`, passed
    to `authorize_call_data_access()` explicitly, never derived from this
    context (`voiceagent.tenancy.context.TenantContext`'s own module
    docstring: "Phase 2 adds one more source: the call-session context the
    Call Orchestrator builds server-side")."""
    return TenantContext(tenant_id=tenant_id, actor_id=uuid.uuid4(), membership_id=uuid.uuid4())


def _engine_provider_name(agent_version: AgentVersion) -> str:
    """Mirrors `voiceagent.runtime.call_task._engine_provider_name()`
    exactly -- duplicated rather than imported across modules for a private
    four-line helper neither module has any other reason to share."""
    engine_config = agent_version.config.get("engine") or {}
    kind = engine_config.get("kind", "pipelined")
    component = engine_config.get(kind) or {}
    return str(component.get("provider", "fake"))


def _engine_selection(agent_version: AgentVersion) -> EngineSelection:
    """The one thing this orchestrator must build that `run_call_task()`
    does not: the actual `ConversationEngine` object `CallTaskDependencies
    .engine` needs, from `AgentVersion.config["engine"]`. `run_call_task()`
    reloads its own `AgentVersion` snapshot and builds its own,
    differently-shaped `EngineSessionConfig` (the per-turn instructions/
    voice/tools) internally -- these are two different config objects for
    two different purposes, and this module needs only this one."""
    return EngineSelection.model_validate(agent_version.config["engine"])


class CallOrchestrator:
    """Coordinates the existing components named in this module's own
    docstring for exactly one thing: turning a normalized, unrouted inbound
    `OFFERED` telephony event into a running, authorized `run_call_task()`
    -- or a clean, bounded rejection. Construct one per `CallRuntime`
    process; wire it in via `TelephonyEventRouter(telephony,
    on_unrouted_offer=orchestrator.handle_unrouted_offer)`.
    """

    def __init__(
        self,
        *,
        db: DatabaseBoundary,
        call_runtime: CallRuntime,
        telephony: _MediaStreamCapableTelephonyProvider,
        media: MediaProvider,
        telephony_events: TelephonyEventRouter,
        heartbeat_store: HeartbeatStore,
        policy_source: AiDataPolicySource,
        tool_gateway: ToolGateway,
        conversation_persistence: ConversationPersistence,
        system_actor_user_id: uuid.UUID | None,
        system_service_account_name: str,
        answer_timeout_seconds: float = 15.0,
        engine_close_timeout_seconds: float = 5.0,
        media_detach_timeout_seconds: float = 5.0,
    ) -> None:
        self._db = db
        self._call_runtime = call_runtime
        self._telephony = telephony
        self._media = media
        self._telephony_events = telephony_events
        self._heartbeat_store = heartbeat_store
        self._policy_source = policy_source
        self._tool_gateway = tool_gateway
        self._conversation_persistence = conversation_persistence
        self._system_actor_user_id = system_actor_user_id
        self._system_service_account_name = system_service_account_name
        self._answer_timeout_seconds = answer_timeout_seconds
        self._engine_close_timeout_seconds = engine_close_timeout_seconds
        self._media_detach_timeout_seconds = media_detach_timeout_seconds
        # Phase 2.22 brief section 13: "no leaked asyncio tasks" -- every
        # background task this orchestrator spawns is tracked here and kept
        # referenced until it finishes (an untracked `asyncio.create_task()`
        # result can be garbage-collected mid-flight, silently cancelling
        # it).
        self._background_tasks: set[asyncio.Task[None]] = set()

    def handle_unrouted_offer(self, event: CallEvent) -> None:
        """The synchronous callback `TelephonyEventRouter(on_unrouted_offer=
        ...)` calls. Never blocks, never awaits: schedules the real
        (database-touching, potentially slow) handling as its own tracked
        background task and returns immediately, so the router's own event
        loop is never delayed by one call's routing work."""
        task = asyncio.create_task(self._handle_offer(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def shutdown(self) -> None:
        """Bounded (brief section 13: "cleanup must be bounded"): cancels
        any still-in-flight offer-handling task rather than waiting for an
        unbounded amount of database/telephony work to finish during
        process shutdown."""
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _reject(
        self, call_ref: CallRef, *, outcome: _Outcome, cause: HangupCause = HangupCause.REJECTED
    ) -> None:
        record_orchestration_event(outcome)
        with contextlib.suppress(TelephonyError):
            await self._telephony.hangup(call_ref, cause)

    async def _handle_offer(self, event: CallEvent) -> None:  # noqa: C901 -- one linear flow;
        # see module docstring for why authorization ordering must not be
        # split across helper functions a future edit could reorder.
        record_orchestration_event("started")
        call_ref = event.call_ref
        try:
            route = await self._db.run(resolve_inbound_route, event.to_number or "")
            if route is None:
                _logger.info(
                    "orchestration.unknown_route", extra={"event": "orchestration.unknown_route"}
                )
                await self._reject(call_ref, outcome="unknown_route")
                return
            if not route.inbound_enabled:
                await self._reject(call_ref, outcome="route_disabled")
                return

            context = _build_system_context(route.tenant_id)

            agent: Agent | None = None
            if route.agent_id is not None:
                try:
                    agent = await self._db.run(get_agent, context, route.agent_id)
                except AgentNotFoundError:
                    agent = None
            if agent is None or agent.status != "active":
                await self._reject(call_ref, outcome="unknown_agent")
                return

            try:
                agent_version_id = select_agent_version_id(
                    agent, pin_mode="follow_published", pinned_version_id=None
                )
                agent_version = await self._db.run(get_agent_version, context, agent_version_id)
            except AgentVersionNotFoundError:
                await self._reject(call_ref, outcome="agent_version_unavailable")
                return

            call = await self._db.run(
                create_call_session,
                context,
                direction="inbound",
                from_e164=event.from_number or "",
                to_e164=event.to_number or "",
                phone_number_id=route.phone_number_id,
                agent_id=agent.id,
                agent_version_id=agent_version.id,
                fs_channel_uuid=call_ref,
            )
            if call.status != "initiated":
                # Brief section 14: a terminal call must not restart, and a
                # replayed/duplicated OFFERED for a call already past
                # `initiated` (already being handled by this or another
                # process, or already finished) needs no further action. A
                # plain snapshot check is sufficient *here* (unlike the
                # activation claim below): it only needs to short-circuit
                # the common case cheaply, before spending an authorization/
                # assignment round-trip on a call that is obviously already
                # past this point -- it is not this check's job to be the
                # exclusive gate (see `claim_call_for_activation()` below for
                # why one is still needed after this).
                record_orchestration_event("duplicate_ignored")
                return

            try:
                await self._db.run(
                    authorize_call_data_access,
                    tenant_id=context.tenant_id,
                    call_session_id=call.id,
                    data_classification=agent_version.config["privacy"]["data_classification"],
                    purpose=agent_version.config["privacy"]["purpose"],
                    provider=_engine_provider_name(agent_version),
                    policy_source=self._policy_source,
                    system_actor_user_id=self._system_actor_user_id,
                )
            except DataAuthorizationDeniedError:
                await self._db.run(
                    transition_call_session,
                    context,
                    call.id,
                    to_status="failed",
                    end_reason="authorization_denied",
                )
                await self._reject(call_ref, outcome="authorization_denied")
                return

            heartbeats = await self._heartbeat_store.read_all()
            try:
                call, winning_instance_id = await self._db.run(
                    assign_call_to_runtime, context, call.id, heartbeats
                )
            except NoRuntimeCapacityError:
                await self._db.run(
                    transition_call_session,
                    context,
                    call.id,
                    to_status="failed",
                    end_reason="no_capacity",
                )
                await self._reject(call_ref, outcome="no_capacity")
                return
            except CallSessionAlreadyOwnedError:
                # A different runtime in the fleet already won this call's
                # ownership claim (this process's own view of `heartbeats`
                # raced a different process's) -- that runtime's own
                # orchestrator is independently processing the identical
                # broadcast event and will proceed from here itself.
                record_orchestration_event("ownership_lost")
                return

            if winning_instance_id != self._call_runtime.instance_id:
                # This process computed a winner, successfully claimed
                # ownership for it, and that winner is not itself -- a
                # different process's `CallRuntime` owns this call now (see
                # module docstring: every process reaches this same
                # conclusion about itself independently). Deliberately *not*
                # where `claim_call_for_activation()` runs (see below): at
                # most one process's own instance_id can ever equal the one
                # deterministic `winning_instance_id` every process computes
                # from the same heartbeats, so cross-process duplication is
                # already impossible past this point without it.
                record_orchestration_event("ownership_lost")
                return

            claimed = await self._db.run(claim_call_for_activation, context, call.id)
            if not claimed:
                # The one race the ownership check above does *not* close:
                # two truly concurrent duplicate events handled by *this
                # same* process both independently compute themselves as
                # `winning_instance_id` (`claim_runtime_ownership()`'s own
                # documented same-instance-reclaim idempotency lets both
                # succeed) and both reach this exact line. Placed here --
                # after ownership is confirmed, not right after
                # `create_call_session()` -- so it never gates the *correct*
                # owning process out of its own call: the earlier placement
                # tried during this phase's own development let an
                # activation claim won by chance by the *non-owning*
                # process's handler strand the call, since only the
                # genuinely owning process ever reaches `assign_call_to_runtime`
                # far enough to call `answer()` (caught by
                # `test_two_runtimes_race_the_same_call_only_one_wins_ownership`,
                # which timed out under the earlier placement).
                record_orchestration_event("duplicate_ignored")
                return

            await self._activate(context, call, agent_version, event)
        except Exception:  # noqa: BLE001 -- one call's own routing bug must
            # never crash this process's orchestrator or another call's
            # handling; logged, counted, and the channel is left for
            # FreeSWITCH's own timeout/cleanup rather than hung up blindly
            # from a code path that may not know the call is even still
            # live.
            record_orchestration_event("error")
            _logger.exception("orchestration.unhandled_error")

    async def _activate(
        self,
        context: TenantContext,
        call: CallSession,
        agent_version: AgentVersion,
        event: CallEvent,
    ) -> None:
        """Everything from "ownership won" through handing the call to
        `CallRuntime.start_call()` -- media/AI processing must never start
        before this point is reached (authorization and ownership are both
        already resolved by the time this is called)."""
        call_ref = event.call_ref
        answer_queue = self._telephony_events.subscribe(call_ref)
        try:
            await self._telephony.answer(call_ref)
            await self._telephony.start_media_stream(call_ref)
            await asyncio.wait_for(
                _wait_for_answered(answer_queue), timeout=self._answer_timeout_seconds
            )
        except (TransportError, TimeoutError):
            await self._db.run(
                transition_call_session,
                context,
                call.id,
                to_status="failed",
                end_reason="media_unavailable",
                expected_runtime_instance_id=self._call_runtime.instance_id,
            )
            await self._reject(call_ref, outcome="media_unavailable")
            return
        finally:
            self._telephony_events.unsubscribe(call_ref)

        await self._db.run(
            transition_call_session,
            context,
            call.id,
            to_status="answered",
            expected_runtime_instance_id=self._call_runtime.instance_id,
        )

        engine = build_conversation_engine(_engine_selection(agent_version))
        deps = CallTaskDependencies(
            engine=engine,
            media=self._media,
            telephony=self._telephony,
            db=self._db,
            policy_source=self._policy_source,
            tool_gateway=self._tool_gateway,
            conversation_persistence=self._conversation_persistence,
            system_actor_user_id=self._system_actor_user_id,
            system_service_account_name=self._system_service_account_name,
            runtime_instance_id=self._call_runtime.instance_id,
            engine_close_timeout_seconds=self._engine_close_timeout_seconds,
            media_detach_timeout_seconds=self._media_detach_timeout_seconds,
            telephony_events=self._telephony_events,
        )
        record_orchestration_event("call_started")
        await self._call_runtime.start_call(context, call.id, call_ref, deps)


async def _wait_for_answered(queue: asyncio.Queue[CallEvent]) -> None:
    while True:
        event = await queue.get()
        if event.type is CallEventType.ANSWERED:
            return
