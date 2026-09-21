"""`ToolGateway` -- the single mediation point between a `ConversationEngine`
and every application capability this product exposes to an AI agent
(ADR-0003 point 1; Phase 2.4).

```text
ToolCallRequested
    |
    v
resolve ToolDefinition        (TOOL_REGISTRY; unknown tool id -> fail closed)
    |
verify AgentVersion allowlist (voiceagent.tools.allowlist; not listed -> deny)
    |
idempotency replay check      (bounded, per-call, in-memory -- see below)
    |
validate input                (ToolDefinition.input_model; extra="forbid")
    |
verify authorization          (core.rbac.can(), via DatabaseBoundary)
    |
timed execution                (ToolDefinition.handler -> TelephonyProvider)
    |
validate output               (ToolDefinition.output_model)
    |
audit outcome                 (core.audit_log.record(), via DatabaseBoundary)
    |
    v
ToolResult
```

This is ADR-0003 point 7's execution order, narrowed to what Phase 2.4
actually has: no entitlement/quota gate exists yet (`core.usage`/
`core.billing` integration is not part of this phase's scope), so that step
is simply absent, not stubbed. A genuine idempotent *replay* (an already-
answered `call_id` seen again) is answered before validation/authorization
run a second time -- re-running either for a value that will be discarded in
favor of the cached result would be wasted work, not a correctness
requirement; ADR-0003's order still governs the *first* delivery of any
given `call_id`.

**Deliberate deviation from ADR-0003 point 2's idempotency primitive.**
`core.idempotency.run_idempotent(tenant_id, operation, idempotency_key,
fingerprint_payload, business_fn)` requires `business_fn: Callable[[Session],
Mapping[str, object]]` -- synchronous, and run *inside* the same DB
transaction/session `run_idempotent()` opens. A tool handler's real work here
is an async `TelephonyProvider` call (`await ctx.telephony.hangup(...)`,
over ESL) -- there is no way to run that inside a synchronous callback
holding a DB session without either blocking the event loop on network I/O
under an open transaction (exactly what `voiceagent.runtime.db` exists to
prevent) or a second thread-hop the primitive was never designed for. Phase
2.4's brief explicitly permits this: "implement bounded per-call execution
deduplication if that is consistent with the existing runtime architecture
... do not introduce a new durable idempotency table unless the existing
architecture proves it is necessary." `ToolGateway` therefore keeps a
bounded, in-memory `dict[call_session_id, dict[tool_call_id, ToolResult]]`,
cleared for a call the moment `voiceagent.runtime.call_task.run_call_task()`
finishes tearing it down (`forget_call()`), instead.

**The crash/restart boundary, stated plainly:** this dedup state lives only
in the runtime process's memory, for the life of one call task. If the
runtime process crashes mid-tool-call, the in-memory record is gone; on
restart there is no call task left running for that `CallSession` to ever
redeliver a duplicate to in the first place (Phase 2.2 implements no crash
takeover -- ADR-0008 point 10, Phase 2.0 report OQ-4 remains open). This is
not a regression Phase 2.4 introduces: it is the same boundary every other
in-memory, one-call-task-lifetime piece of state in this runtime already has
(`PipelinedEngineSession._messages`, `CallRuntime`'s own task table). A
future durable idempotency store is exactly the kind of change OQ-4's
eventual resolution would need to reconsider, not something to build ahead
of that decision.

**Authorization uses `core.rbac.can()` directly, not a second RBAC system**
(ADR-0003 point 2). The acting principal is the call runtime's own, *per
tenant*, `core.identity.ServiceAccount` -- resolved by name
(`RuntimeSettings.system_service_account_name`) for the call's own tenant at
execution time, since a `ServiceAccount`'s `tenant_id` is fixed permanently
at creation and one global id cannot authorize calls across more than one
tenant (see `_resolve_service_account()` and `RuntimeSettings
.system_service_account_name`'s own docstring). Never a user, and never,
ever a principal identifier taken from `ToolCallRequested.arguments` (Phase
2.4 brief section 7).

**Audit uses `core.audit_log.record()`, not a new table.** ADR-0003 point 7's
"persisted `ToolCall` record" is satisfied by one `core.audit_log` entry per
terminal outcome (denied/validation_failed/succeeded/failed/timed_out/
cancelled/duplicate) -- `docs/PHASE-2.4-STATUS.md` §"ADR-0003 clarification"
records this as the intended reading now that Phase 2.4's own brief
prohibits a new domain table for something `core.audit_log` already
persists. Tool arguments are never included in audit metadata (brief section
11: "be careful with transfer destinations and tool arguments") -- metadata
carries `tool_id`, `call_session_id` and the granular status only.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping

from core.audit_log import ActorType, AuditOutcome
from core.audit_log import record as record_audit_event
from core.identity import ServiceAccountStatus, list_service_accounts
from core.rbac import PrincipalType
from core.rbac import can as rbac_can
from pydantic import ValidationError

from voiceagent.agents.models import AgentVersion
from voiceagent.observability import bind_correlation_context
from voiceagent.providers.engines.contracts import ToolCallRequested, ToolResult
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.telephony.contracts import CallRef, TelephonyProvider
from voiceagent.tenancy import TenantContext
from voiceagent.tools.allowlist import is_tool_allowed
from voiceagent.tools.definitions import StrictToolModel, ToolDefinition, ToolExecutionContext
from voiceagent.tools.errors import (
    ToolExecutionError,
    UnknownToolError,
)
from voiceagent.tools.registry import TOOL_REGISTRY, ToolRegistry

__all__ = ["RESOURCE", "ToolGateway"]

_logger = logging.getLogger(__name__)

#: Matches `voiceagent.tools.permissions.RESOURCE` -- see that module's
#: docstring for why it is one constant, re-exported, rather than two string
#: literals.
RESOURCE = "voiceagent.tools"


def _audit(
    *,
    tenant_id: uuid.UUID,
    call_session_id: uuid.UUID,
    tool_id: str,
    status: str,
    outcome: AuditOutcome,
    actor_service_account_id: uuid.UUID | None,
) -> None:
    """Synchronous -- always called through `DatabaseBoundary.run()`, never
    directly from `execute()`'s own coroutine body.

    `actor_service_account_id=None` attributes the entry to
    `ActorType.SYSTEM` instead -- used only when no service account could be
    resolved for `tenant_id` at all (`_resolve_service_account()` below):
    attributing to *some* service account id in that case would mean either
    fabricating one or reusing one from a different tenant, and
    `core.audit_log`'s own `fk_audit_log_tenant_service_account` constraint
    correctly rejects the latter (Phase 2.4's own integration testing hit
    this -- see `docs/PHASE-2.4-STATUS.md`).
    """
    record_audit_event(
        tenant_id=tenant_id,
        actor_type=(
            ActorType.SERVICE_ACCOUNT if actor_service_account_id is not None else ActorType.SYSTEM
        ),
        actor_service_account_id=actor_service_account_id,
        action=f"tool.{tool_id}",
        resource_type="tool_call",
        resource_id=str(call_session_id),
        outcome=outcome,
        correlation_id=str(call_session_id),
        metadata={"tool_id": tool_id, "status": status, "call_session_id": str(call_session_id)},
    )


def _resolve_service_account(*, tenant_id: uuid.UUID, name: str) -> uuid.UUID | None:
    """Synchronous -- always called through `DatabaseBoundary.run()`.

    Resolves the *tenant-local* `core.identity.ServiceAccount` the runtime
    acts as for this one call, by name (`RuntimeSettings
    .system_service_account_name`) -- never a fixed, cross-tenant id. See
    `RuntimeSettings.system_service_account_name`'s own docstring for why a
    single global id cannot work here: a `ServiceAccount`'s `tenant_id` is
    fixed permanently at creation, so authorizing calls across more than one
    tenant requires one row per tenant, found by a name they share.

    Returns `None` if no `ACTIVE` service account with this name exists for
    `tenant_id` -- a real, expected outcome for a tenant an operator has not
    yet run `scripts/bootstrap_rbac.py` for, not a programming error.
    """
    for account in list_service_accounts(tenant_id):
        if account.name == name and account.status == ServiceAccountStatus.ACTIVE.value:
            return account.id
    return None


def _check_authorization(
    *, tenant_id: uuid.UUID, service_account_id: uuid.UUID, permission_action: str
) -> bool:
    """Synchronous -- always called through `DatabaseBoundary.run()`.

    `actor_tenant_id=tenant_id`: `core.rbac.can()` requires it for a
    `SERVICE_ACCOUNT` actor -- "the service account's own, single, fixed
    tenant" (that function's own docstring) -- and fails closed without it,
    unconditionally. `service_account_id` is already resolved, by
    `_resolve_service_account()` above, to a row that belongs to `tenant_id`,
    so the call's tenant genuinely *is* the service account's own tenant
    here.
    """
    return rbac_can(
        actor_id=service_account_id,
        tenant_id=tenant_id,
        action=permission_action,
        resource=RESOURCE,
        actor_type=PrincipalType.SERVICE_ACCOUNT,
        actor_tenant_id=tenant_id,
    )


class ToolGateway:
    """Provider-neutral. Never imports, or is imported by, any
    `voiceagent.providers.engines.pipelined`/`realtime` module -- the engine
    calls this class through `voiceagent.runtime.call_task` only, and never
    learns which application service implements a tool
    (`tests/architecture/test_tool_gateway_isolation.py`).
    """

    def __init__(self, registry: ToolRegistry = TOOL_REGISTRY) -> None:
        self._registry = registry
        self._results: dict[uuid.UUID, dict[str, ToolResult]] = {}

    def forget_call(self, call_session_id: uuid.UUID) -> None:
        """Drop this call's idempotency cache. Called once, from
        `voiceagent.runtime.call_task.run_call_task()`'s own teardown --
        never from inside the audio pump -- so dedup state never outlives
        the one call task it was collected for (see module docstring's
        "bounded" claim)."""
        self._results.pop(call_session_id, None)

    async def execute(
        self,
        *,
        db: DatabaseBoundary,
        context: TenantContext,
        call_session_id: uuid.UUID,
        agent_version: AgentVersion,
        call_ref: CallRef,
        telephony: TelephonyProvider,
        system_service_account_name: str,
        request: ToolCallRequested,
    ) -> ToolResult:
        with bind_correlation_context(tenant_id=str(context.tenant_id), request_id=request.call_id):
            # Resolved once per call, up front: every audit write below
            # attributes to this tenant's own service account (or, if none
            # is provisioned yet, to ActorType.SYSTEM -- see _audit()'s own
            # docstring for why a cross-tenant id is never used instead).
            service_account_id = await db.run(
                _resolve_service_account,
                tenant_id=context.tenant_id,
                name=system_service_account_name,
            )

            cached = self._results.get(call_session_id, {}).get(request.call_id)
            if cached is not None:
                await db.run(
                    _audit,
                    tenant_id=context.tenant_id,
                    call_session_id=call_session_id,
                    tool_id=request.name,
                    status="duplicate",
                    outcome=AuditOutcome.SUCCESS,
                    actor_service_account_id=service_account_id,
                )
                return cached

            try:
                definition = self._registry.resolve(request.name)
            except UnknownToolError:
                await db.run(
                    _audit,
                    tenant_id=context.tenant_id,
                    call_session_id=call_session_id,
                    tool_id=request.name,
                    status="denied",
                    outcome=AuditOutcome.DENIED,
                    actor_service_account_id=service_account_id,
                )
                return self._remember(
                    call_session_id,
                    ToolResult(call_id=request.call_id, error_code="unknown_tool", retryable=False),
                )

            if not is_tool_allowed(agent_version, definition.tool_id):
                await db.run(
                    _audit,
                    tenant_id=context.tenant_id,
                    call_session_id=call_session_id,
                    tool_id=definition.tool_id,
                    status="denied",
                    outcome=AuditOutcome.DENIED,
                    actor_service_account_id=service_account_id,
                )
                return self._remember(
                    call_session_id,
                    ToolResult(
                        call_id=request.call_id, error_code="tool_not_allowed", retryable=False
                    ),
                )

            try:
                typed_input = definition.input_model.model_validate(request.arguments)
            except ValidationError:
                await db.run(
                    _audit,
                    tenant_id=context.tenant_id,
                    call_session_id=call_session_id,
                    tool_id=definition.tool_id,
                    status="validation_failed",
                    outcome=AuditOutcome.FAILURE,
                    actor_service_account_id=service_account_id,
                )
                return self._remember(
                    call_session_id,
                    ToolResult(
                        call_id=request.call_id, error_code="invalid_arguments", retryable=False
                    ),
                )

            authorized = service_account_id is not None and await db.run(
                _check_authorization,
                tenant_id=context.tenant_id,
                service_account_id=service_account_id,
                permission_action=definition.permission_action,
            )
            if not authorized:
                await db.run(
                    _audit,
                    tenant_id=context.tenant_id,
                    call_session_id=call_session_id,
                    tool_id=definition.tool_id,
                    status="denied",
                    outcome=AuditOutcome.DENIED,
                    actor_service_account_id=service_account_id,
                )
                return self._remember(
                    call_session_id,
                    ToolResult(call_id=request.call_id, error_code="unauthorized", retryable=False),
                )

            await db.run(
                _audit,
                tenant_id=context.tenant_id,
                call_session_id=call_session_id,
                tool_id=definition.tool_id,
                status="started",
                outcome=AuditOutcome.SUCCESS,
                actor_service_account_id=service_account_id,
            )

            exec_context = ToolExecutionContext(
                tenant_id=context.tenant_id,
                call_session_id=call_session_id,
                agent_version_id=agent_version.id,
                tool_call_id=request.call_id,
                correlation_id=request.call_id,
                call_ref=call_ref,
                telephony=telephony,
            )
            result = await self._run_handler(
                db=db,
                context=context,
                call_session_id=call_session_id,
                definition=definition,
                exec_context=exec_context,
                typed_input=typed_input,
                request=request,
                service_account_id=service_account_id,
            )
            return self._remember(call_session_id, result)

    async def _run_handler(
        self,
        *,
        db: DatabaseBoundary,
        context: TenantContext,
        call_session_id: uuid.UUID,
        definition: ToolDefinition,
        exec_context: ToolExecutionContext,
        typed_input: StrictToolModel,
        request: ToolCallRequested,
        service_account_id: uuid.UUID | None,
    ) -> ToolResult:
        try:
            raw_output = await asyncio.wait_for(
                definition.handler(exec_context, typed_input), timeout=definition.timeout_seconds
            )
        except TimeoutError:
            await db.run(
                _audit,
                tenant_id=context.tenant_id,
                call_session_id=call_session_id,
                tool_id=definition.tool_id,
                status="timed_out",
                outcome=AuditOutcome.FAILURE,
                actor_service_account_id=service_account_id,
            )
            return ToolResult(call_id=request.call_id, error_code="timeout", retryable=True)
        except asyncio.CancelledError:
            await db.run(
                _audit,
                tenant_id=context.tenant_id,
                call_session_id=call_session_id,
                tool_id=definition.tool_id,
                status="cancelled",
                outcome=AuditOutcome.FAILURE,
                actor_service_account_id=service_account_id,
            )
            raise
        except ToolExecutionError as exc:
            await db.run(
                _audit,
                tenant_id=context.tenant_id,
                call_session_id=call_session_id,
                tool_id=definition.tool_id,
                status="failed",
                outcome=AuditOutcome.FAILURE,
                actor_service_account_id=service_account_id,
            )
            return ToolResult(call_id=request.call_id, error_code=exc.code, retryable=exc.retryable)
        except Exception:  # noqa: BLE001 -- a handler bug must become a
            # normalized failure at the model boundary (ADR-0003 point 8),
            # never a raw traceback the model or the call task sees; logged,
            # not silently dropped, so it is at least operationally visible.
            _logger.exception(
                "tool handler %s raised an unnormalized exception", definition.tool_id
            )
            await db.run(
                _audit,
                tenant_id=context.tenant_id,
                call_session_id=call_session_id,
                tool_id=definition.tool_id,
                status="failed",
                outcome=AuditOutcome.FAILURE,
                actor_service_account_id=service_account_id,
            )
            return ToolResult(call_id=request.call_id, error_code="internal_error", retryable=False)

        try:
            validated = definition.output_model.model_validate(raw_output)
        except ValidationError:
            _logger.error(
                "tool handler %s returned output that failed its own output_model",
                definition.tool_id,
            )
            await db.run(
                _audit,
                tenant_id=context.tenant_id,
                call_session_id=call_session_id,
                tool_id=definition.tool_id,
                status="failed",
                outcome=AuditOutcome.FAILURE,
                actor_service_account_id=service_account_id,
            )
            return ToolResult(call_id=request.call_id, error_code="internal_error", retryable=False)

        await db.run(
            _audit,
            tenant_id=context.tenant_id,
            call_session_id=call_session_id,
            tool_id=definition.tool_id,
            status="succeeded",
            outcome=AuditOutcome.SUCCESS,
            actor_service_account_id=service_account_id,
        )
        value: Mapping[str, object] = validated.model_dump(mode="json")
        return ToolResult(call_id=request.call_id, value=value)

    def _remember(self, call_session_id: uuid.UUID, result: ToolResult) -> ToolResult:
        self._results.setdefault(call_session_id, {})[result.call_id] = result
        return result
