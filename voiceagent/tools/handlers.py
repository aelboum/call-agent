"""Phase 2.4's four built-in call-control tools, plus Phase 2.6's four
Contact/Calendar tools, plus Phase 2.7's two Call Outcome/Follow-up tools,
plus Phase 2.10's one controlled-workflow tool (`workflow.advance`), plus
Phase 2.11's one knowledge tool (`knowledge.search`).

Every Phase 2.4 handler is `Tool -> application capability ->
TelephonyProvider` (brief section 13) -- none of them imports FreeSWITCH/ESL,
`mod_audio_stream`, or anything from `voiceagent.telephony.freeswitch`; each
one calls exactly one `voiceagent.telephony.contracts.TelephonyProvider`
method, already resolved onto this call's own `CallRef` by
`voiceagent.tools.gateway.ToolGateway` before the handler ever runs (never a
destination the model could redirect at a *different* call). `TelephonyProvider`
already has `hangup()`/`transfer()`/`hold()`/`unhold()` (Phase 2.2) -- no
contract change was needed to add these four tools.

`call.transfer`'s `destination_e164` is validated by `TransferInput`'s own
`Field(pattern=...)` before the handler -- or the ESL command string
`FreeSwitchTelephonyProvider.transfer()` builds by direct interpolation --
ever sees it (`voiceagent/telephony/freeswitch/provider.py`: `f"bgapi
originate sofia/gateway/default/{destination}"`). This is the concrete
defense the brief's "never accept arbitrary executable destinations" asks
for: a `TransferInput` that fails E.164 validation never reaches that
f-string.

**Phase 2.6's four handlers are `Tool -> application service ->
DatabaseBoundary`, never `Tool -> TelephonyProvider`**: `contact.lookup_by_phone`,
`calendar.check_availability`, `calendar.create_appointment` and
`calendar.cancel_appointment` call `voiceagent.contacts.service`/
`voiceagent.calendars.service` through `ctx.db.run(...)`
(`voiceagent.runtime.db.DatabaseBoundary` -- see `ToolExecutionContext`'s own
docstring for why `tenant_context`/`db` exist on it at all), exactly the
bounded-thread-pool boundary the runtime's own `load`/`authorize`/`finalize`
steps already cross, and never touch `voiceagent.db`/
`voiceagent.tenancy.tenant_scope` directly from this module (brief §17;
`tests/architecture/test_tool_gateway_isolation.py
::test_no_tool_module_imports_voiceagent_db_directly`). Every domain error
these two services can raise is caught here and normalized to a fixed
`ToolExecutionError` code -- never a raw exception, and never one that echoes
tenant-sensitive content (name/phone/email/title) into its message (brief
§19).

**Phase 2.7's two handlers, `call.set_outcome` and `call.create_follow_up`,
follow the identical `Tool -> application service -> DatabaseBoundary`
shape**, calling `voiceagent.followups.service` -- the appointment
orchestration for a `type="appointment"` follow-up (calling
`voiceagent.calendars.service` when needed) lives inside that service
module itself, not here, so this handler stays a thin translation layer
exactly like every other tool handler in this file. Neither new input model
carries a `call_id`/`call_session_id` field: both tools act on *this* call
only, taken from `ctx.call_session_id`, never from model output (ADR-0003
point 4 -- the same rule `HangupInput`'s own docstring states for
`call.hangup`). See `voiceagent.followups.service`'s own module docstring
for why neither input model carries a `contact_id` field either.

**Phase 2.10's one handler, `workflow.advance`, is `Tool -> voiceagent
.workflows.executor.run_workflow() -> {application service, nested Tool
Gateway call}`.** It takes no arguments (the workflow it advances is always
the one published on *this* call's own `AgentVersion`) and is the only
handler in this file that reads `ctx.agent_version`/`ctx.tool_gateway`/
`ctx.system_service_account_name` -- see `ToolExecutionContext`'s own
docstring for why those three exist at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

from pydantic import AwareDatetime, Field

from voiceagent.calendars import service as calendar_service
from voiceagent.calendars.errors import (
    CalendarEventConflictError,
    CalendarEventNotFoundError,
    CalendarNotFoundError,
    InvalidIntervalError,
    NaiveDatetimeError,
)
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.contacts import service as contact_service
from voiceagent.contacts.errors import ContactNotFoundError
from voiceagent.followups import service as followup_service
from voiceagent.followups.errors import (
    FollowUpAppointmentRequiresCalendarEventError,
    FollowUpInvalidRelationshipError,
    InvalidFollowUpTypeError,
    InvalidOutcomeValueError,
)
from voiceagent.knowledge.errors import InvalidKnowledgeQueryError
from voiceagent.knowledge.retrieval import MAX_QUERY_LENGTH, MAX_RESULT_LIMIT, search_items
from voiceagent.telephony.contracts import HangupCause, TelephonyError
from voiceagent.tools.definitions import (
    StrictToolModel,
    ToolDefinition,
    ToolExecutionContext,
    ToolRisk,
)
from voiceagent.tools.errors import ToolExecutionError
from voiceagent.tools.registry import TOOL_REGISTRY
from voiceagent.workflows.config import WORKFLOW_TOOL_ID
from voiceagent.workflows.executor import run_workflow

if TYPE_CHECKING:
    from voiceagent.agents.models import AgentVersion
    from voiceagent.runtime.db import DatabaseBoundary
    from voiceagent.tenancy import TenantContext
    from voiceagent.tools.gateway import ToolGateway

__all__ = [
    "AppointmentRef",
    "CancelAppointmentInput",
    "CancelAppointmentOutput",
    "CheckAvailabilityInput",
    "CheckAvailabilityOutput",
    "ContactRef",
    "CreateAppointmentInput",
    "CreateAppointmentOutput",
    "CreateFollowUpInput",
    "CreateFollowUpOutput",
    "FollowUpRef",
    "HangupInput",
    "HangupOutput",
    "HoldInput",
    "HoldOutput",
    "KnowledgeSearchInput",
    "KnowledgeSearchOutput",
    "KnowledgeSearchResultRef",
    "LookupContactByPhoneInput",
    "LookupContactByPhoneOutput",
    "ResumeInput",
    "ResumeOutput",
    "SetOutcomeInput",
    "SetOutcomeOutput",
    "TransferInput",
    "TransferOutput",
    "WorkflowAdvanceInput",
    "WorkflowAdvanceOutput",
]

#: E.164: a leading `+`, a non-zero first digit, up to 15 digits total.
_E164_PATTERN = r"^\+[1-9]\d{1,14}$"

#: Every telephony operation gets the same bound -- none of the four is
#: expected to take more than an ESL round trip; a stuck FreeSWITCH command
#: must not hold a call task's tool-result future open indefinitely.
_TIMEOUT_SECONDS = 10.0


def _telephony_failure(exc: TelephonyError) -> ToolExecutionError:
    """Normalize any `TelephonyError` the same way, across all four
    handlers -- never let a vendor/transport-shaped exception (or its
    message, which may embed a raw ESL response) become the tool's own
    error, only a fixed, non-leaking code (ADR-0003 point 8)."""
    return ToolExecutionError("telephony_error", "the telephony provider rejected the operation")


# -- call.hangup --------------------------------------------------------


class HangupInput(StrictToolModel):
    """No arguments: hangup always targets *this* call, never one named by
    the model."""


class HangupOutput(StrictToolModel):
    hung_up: Literal[True] = True


async def _hangup(ctx: ToolExecutionContext, _input: StrictToolModel) -> dict[str, object]:
    try:
        await ctx.telephony.hangup(ctx.call_ref, cause=HangupCause.NORMAL)
    except TelephonyError as exc:
        raise _telephony_failure(exc) from exc
    return {"hung_up": True}


# -- call.transfer -------------------------------------------------------


class TransferInput(StrictToolModel):
    destination_e164: str = Field(pattern=_E164_PATTERN)


class TransferOutput(StrictToolModel):
    transferred: Literal[True] = True
    destination_e164: str


async def _transfer(ctx: ToolExecutionContext, tool_input: StrictToolModel) -> dict[str, object]:
    # The gateway always calls a tool's handler with an instance of that
    # tool's own `input_model` (validated by `ToolGateway.execute()`
    # immediately before dispatch) -- `ToolHandler`'s parameter type is
    # `StrictToolModel` only because a single `Callable` type must describe
    # every handler in `TOOL_REGISTRY` uniformly (parameter contravariance
    # otherwise makes a heterogeneous registry of specifically-typed
    # handlers unrepresentable). This cast documents, rather than works
    # around, that runtime guarantee.
    destination = cast(TransferInput, tool_input).destination_e164
    try:
        await ctx.telephony.transfer(ctx.call_ref, destination)
    except TelephonyError as exc:
        raise _telephony_failure(exc) from exc
    return {"transferred": True, "destination_e164": destination}


# -- call.hold -------------------------------------------------------------


class HoldInput(StrictToolModel):
    """No arguments: hold always targets *this* call."""


class HoldOutput(StrictToolModel):
    held: Literal[True] = True


async def _hold(ctx: ToolExecutionContext, _input: StrictToolModel) -> dict[str, object]:
    try:
        await ctx.telephony.hold(ctx.call_ref)
    except TelephonyError as exc:
        raise _telephony_failure(exc) from exc
    return {"held": True}


# -- call.resume -------------------------------------------------------------


class ResumeInput(StrictToolModel):
    """No arguments: resume always targets *this* call."""


class ResumeOutput(StrictToolModel):
    resumed: Literal[True] = True


async def _resume(ctx: ToolExecutionContext, _input: StrictToolModel) -> dict[str, object]:
    try:
        await ctx.telephony.unhold(ctx.call_ref)
    except TelephonyError as exc:
        raise _telephony_failure(exc) from exc
    return {"resumed": True}


# -- registration ------------------------------------------------------------

TOOL_REGISTRY.register(
    ToolDefinition(
        tool_id="call.hangup",
        name="call.hangup",
        description="End the current phone call.",
        input_model=HangupInput,
        output_model=HangupOutput,
        permission_action="call.hangup",
        risk=ToolRisk.HIGH,
        idempotent=True,
        timeout_seconds=_TIMEOUT_SECONDS,
        handler=_hangup,
    )
)
TOOL_REGISTRY.register(
    ToolDefinition(
        tool_id="call.transfer",
        name="call.transfer",
        description="Transfer the current phone call to another E.164 number.",
        input_model=TransferInput,
        output_model=TransferOutput,
        permission_action="call.transfer",
        risk=ToolRisk.HIGH,
        idempotent=True,
        timeout_seconds=_TIMEOUT_SECONDS,
        handler=_transfer,
    )
)
TOOL_REGISTRY.register(
    ToolDefinition(
        tool_id="call.hold",
        name="call.hold",
        description="Place the current phone call on hold.",
        input_model=HoldInput,
        output_model=HoldOutput,
        permission_action="call.hold",
        risk=ToolRisk.LOW,
        idempotent=True,
        timeout_seconds=_TIMEOUT_SECONDS,
        handler=_hold,
    )
)
TOOL_REGISTRY.register(
    ToolDefinition(
        tool_id="call.resume",
        name="call.resume",
        description="Resume a phone call previously placed on hold.",
        input_model=ResumeInput,
        output_model=ResumeOutput,
        permission_action="call.resume",
        risk=ToolRisk.LOW,
        idempotent=True,
        timeout_seconds=_TIMEOUT_SECONDS,
        handler=_resume,
    )
)


# -- Phase 2.6: contact/calendar tools ---------------------------------------

#: A DB-crossing tool waits on a thread-pool round trip, not an ESL command --
#: bounded the same way, but with headroom for a real query under load.
_DB_TIMEOUT_SECONDS = 10.0


def _require_db_context(ctx: ToolExecutionContext) -> tuple[DatabaseBoundary, TenantContext]:
    """Every Phase 2.6 tool is only ever reachable through
    `voiceagent.tools.gateway.ToolGateway.execute()`, which always populates
    `tenant_context`/`db` (see `ToolExecutionContext`'s own docstring) --
    `None` here would be a caller bug, not a real, reachable runtime state.
    Raised as a normalized `ToolExecutionError`, the same as every other
    handler-level failure (ADR-0003 point 8), rather than left to surface as
    an unguarded `AttributeError` a few lines below."""
    if ctx.db is None or ctx.tenant_context is None:
        raise ToolExecutionError("internal_error", "tool execution context is incomplete")
    return ctx.db, ctx.tenant_context


# -- contact.lookup_by_phone -------------------------------------------------


class ContactRef(StrictToolModel):
    contact_id: uuid.UUID
    name: str
    phone_e164: str
    email: str | None = None


class LookupContactByPhoneInput(StrictToolModel):
    phone_e164: str


class LookupContactByPhoneOutput(StrictToolModel):
    found: bool
    contact: ContactRef | None = None


async def _lookup_contact_by_phone(
    ctx: ToolExecutionContext, tool_input: StrictToolModel
) -> dict[str, object]:
    db, tenant_context = _require_db_context(ctx)
    phone_e164 = cast(LookupContactByPhoneInput, tool_input).phone_e164
    contact = await db.run(contact_service.lookup_contact_by_phone, tenant_context, phone_e164)
    if contact is None:
        return {"found": False, "contact": None}
    return {
        "found": True,
        "contact": {
            "contact_id": str(contact.id),
            "name": contact.name,
            "phone_e164": contact.phone_e164,
            "email": contact.email,
        },
    }


# -- calendar.check_availability --------------------------------------------


class CheckAvailabilityInput(StrictToolModel):
    calendar_id: uuid.UUID
    start_at: AwareDatetime
    end_at: AwareDatetime


class CheckAvailabilityOutput(StrictToolModel):
    available: bool
    conflict_start_at: datetime | None = None
    conflict_end_at: datetime | None = None


async def _check_availability(
    ctx: ToolExecutionContext, tool_input: StrictToolModel
) -> dict[str, object]:
    db, tenant_context = _require_db_context(ctx)
    typed_input = cast(CheckAvailabilityInput, tool_input)
    try:
        result = await db.run(
            calendar_service.check_availability,
            tenant_context,
            typed_input.calendar_id,
            typed_input.start_at,
            typed_input.end_at,
        )
    except CalendarNotFoundError as exc:
        raise ToolExecutionError("calendar_not_found", "no such calendar") from exc
    except (InvalidIntervalError, NaiveDatetimeError) as exc:
        raise ToolExecutionError("invalid_interval", "start_at must be before end_at") from exc
    return {
        "available": result.available,
        "conflict_start_at": result.conflict_start_at,
        "conflict_end_at": result.conflict_end_at,
    }


# -- calendar.create_appointment ---------------------------------------------


class AppointmentRef(StrictToolModel):
    event_id: uuid.UUID
    calendar_id: uuid.UUID
    contact_id: uuid.UUID | None
    start_at: datetime
    end_at: datetime
    status: str


class CreateAppointmentInput(StrictToolModel):
    calendar_id: uuid.UUID
    title: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    contact_id: uuid.UUID | None = None


class CreateAppointmentOutput(StrictToolModel):
    appointment: AppointmentRef


async def _create_appointment(
    ctx: ToolExecutionContext, tool_input: StrictToolModel
) -> dict[str, object]:
    db, tenant_context = _require_db_context(ctx)
    typed_input = cast(CreateAppointmentInput, tool_input)
    try:
        event = await db.run(
            calendar_service.create_event,
            tenant_context,
            calendar_id=typed_input.calendar_id,
            title=typed_input.title,
            start_at=typed_input.start_at,
            end_at=typed_input.end_at,
            contact_id=typed_input.contact_id,
        )
    except CalendarNotFoundError as exc:
        raise ToolExecutionError("calendar_not_found", "no such calendar") from exc
    except ContactNotFoundError as exc:
        raise ToolExecutionError("contact_not_found", "no such contact") from exc
    except (InvalidIntervalError, NaiveDatetimeError) as exc:
        raise ToolExecutionError("invalid_interval", "start_at must be before end_at") from exc
    except CalendarEventConflictError as exc:
        raise ToolExecutionError(
            "conflict", "requested interval conflicts with an existing appointment"
        ) from exc
    return {
        "appointment": {
            "event_id": str(event.id),
            "calendar_id": str(event.calendar_id),
            "contact_id": str(event.contact_id) if event.contact_id else None,
            "start_at": event.start_at,
            "end_at": event.end_at,
            "status": event.status,
        }
    }


# -- calendar.cancel_appointment ---------------------------------------------


class CancelAppointmentInput(StrictToolModel):
    event_id: uuid.UUID


class CancelAppointmentOutput(StrictToolModel):
    event_id: uuid.UUID
    status: str


async def _cancel_appointment(
    ctx: ToolExecutionContext, tool_input: StrictToolModel
) -> dict[str, object]:
    db, tenant_context = _require_db_context(ctx)
    event_id = cast(CancelAppointmentInput, tool_input).event_id
    try:
        event = await db.run(calendar_service.cancel_event, tenant_context, event_id)
    except CalendarEventNotFoundError as exc:
        raise ToolExecutionError("event_not_found", "no such appointment") from exc
    return {"event_id": str(event.id), "status": event.status}


# -- registration ------------------------------------------------------------

TOOL_REGISTRY.register(
    ToolDefinition(
        tool_id="contact.lookup_by_phone",
        name="contact.lookup_by_phone",
        description="Look up a contact in this tenant by normalized E.164 phone number.",
        input_model=LookupContactByPhoneInput,
        output_model=LookupContactByPhoneOutput,
        permission_action="contact.lookup_by_phone",
        risk=ToolRisk.LOW,
        idempotent=True,
        timeout_seconds=_DB_TIMEOUT_SECONDS,
        handler=_lookup_contact_by_phone,
    )
)
TOOL_REGISTRY.register(
    ToolDefinition(
        tool_id="calendar.check_availability",
        name="calendar.check_availability",
        description="Check whether a calendar is available for a given interval.",
        input_model=CheckAvailabilityInput,
        output_model=CheckAvailabilityOutput,
        permission_action="calendar.check_availability",
        risk=ToolRisk.LOW,
        idempotent=True,
        timeout_seconds=_DB_TIMEOUT_SECONDS,
        handler=_check_availability,
    )
)
TOOL_REGISTRY.register(
    ToolDefinition(
        tool_id="calendar.create_appointment",
        name="calendar.create_appointment",
        description="Create a new appointment on a calendar, optionally linked to a contact.",
        input_model=CreateAppointmentInput,
        output_model=CreateAppointmentOutput,
        permission_action="calendar.create_appointment",
        risk=ToolRisk.LOW,
        idempotent=False,
        timeout_seconds=_DB_TIMEOUT_SECONDS,
        handler=_create_appointment,
    )
)
TOOL_REGISTRY.register(
    ToolDefinition(
        tool_id="calendar.cancel_appointment",
        name="calendar.cancel_appointment",
        description="Cancel an existing appointment.",
        input_model=CancelAppointmentInput,
        output_model=CancelAppointmentOutput,
        permission_action="calendar.cancel_appointment",
        risk=ToolRisk.LOW,
        idempotent=True,
        timeout_seconds=_DB_TIMEOUT_SECONDS,
        handler=_cancel_appointment,
    )
)


# -- Phase 2.7: call outcome / follow-up tools -------------------------------


# -- call.set_outcome ---------------------------------------------------------


class SetOutcomeInput(StrictToolModel):
    outcome: Literal[
        "resolved",
        "appointment_scheduled",
        "follow_up_required",
        "no_answer",
        "wrong_number",
        "not_interested",
    ]
    notes: str | None = None


class SetOutcomeOutput(StrictToolModel):
    call_session_id: uuid.UUID
    outcome: str


async def _set_outcome(ctx: ToolExecutionContext, tool_input: StrictToolModel) -> dict[str, object]:
    db, tenant_context = _require_db_context(ctx)
    typed_input = cast(SetOutcomeInput, tool_input)
    try:
        outcome = await db.run(
            followup_service.set_call_outcome,
            tenant_context,
            ctx.call_session_id,
            outcome=typed_input.outcome,
            notes=typed_input.notes,
        )
    except CallSessionNotFoundError as exc:
        raise ToolExecutionError("call_not_found", "no such call session") from exc
    except InvalidOutcomeValueError as exc:
        raise ToolExecutionError("invalid_outcome", "invalid outcome value") from exc
    return {"call_session_id": str(outcome.call_session_id), "outcome": outcome.outcome}


# -- call.create_follow_up ----------------------------------------------------


class CreateFollowUpInput(StrictToolModel):
    type: Literal["appointment", "contact", "manual_follow_up"]
    description: str | None = None
    due_at: AwareDatetime | None = None
    calendar_id: uuid.UUID | None = None
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None


class FollowUpRef(StrictToolModel):
    follow_up_id: uuid.UUID
    call_session_id: uuid.UUID
    type: str
    status: str
    due_at: datetime | None
    calendar_event_id: uuid.UUID | None
    description: str | None


class CreateFollowUpOutput(StrictToolModel):
    follow_up: FollowUpRef


async def _create_follow_up(
    ctx: ToolExecutionContext, tool_input: StrictToolModel
) -> dict[str, object]:
    db, tenant_context = _require_db_context(ctx)
    typed_input = cast(CreateFollowUpInput, tool_input)
    try:
        follow_up = await db.run(
            followup_service.create_follow_up,
            tenant_context,
            ctx.call_session_id,
            type=typed_input.type,
            description=typed_input.description,
            due_at=typed_input.due_at,
            calendar_id=typed_input.calendar_id,
            start_at=typed_input.start_at,
            end_at=typed_input.end_at,
        )
    except CallSessionNotFoundError as exc:
        raise ToolExecutionError("call_not_found", "no such call session") from exc
    except InvalidFollowUpTypeError as exc:
        raise ToolExecutionError("invalid_type", "invalid follow-up type") from exc
    except FollowUpAppointmentRequiresCalendarEventError as exc:
        raise ToolExecutionError(
            "appointment_requires_calendar_event",
            "an appointment follow-up requires calendar_id/start_at/end_at "
            "or an existing calendar event",
        ) from exc
    except FollowUpInvalidRelationshipError as exc:
        raise ToolExecutionError(
            "invalid_relationship",
            "calendar_event_id is only valid for an appointment follow-up",
        ) from exc
    except CalendarNotFoundError as exc:
        raise ToolExecutionError("calendar_not_found", "no such calendar") from exc
    except ContactNotFoundError as exc:
        raise ToolExecutionError("contact_not_found", "no such contact") from exc
    except (InvalidIntervalError, NaiveDatetimeError) as exc:
        raise ToolExecutionError("invalid_interval", "start_at must be before end_at") from exc
    except CalendarEventConflictError as exc:
        raise ToolExecutionError(
            "conflict", "requested interval conflicts with an existing appointment"
        ) from exc
    return {
        "follow_up": {
            "follow_up_id": str(follow_up.id),
            "call_session_id": str(follow_up.call_session_id),
            "type": follow_up.type,
            "status": follow_up.status,
            "due_at": follow_up.due_at,
            "calendar_event_id": (
                str(follow_up.calendar_event_id) if follow_up.calendar_event_id else None
            ),
            "description": follow_up.description,
        }
    }


# -- registration ------------------------------------------------------------

TOOL_REGISTRY.register(
    ToolDefinition(
        tool_id="call.set_outcome",
        name="call.set_outcome",
        description="Set the business outcome of the current phone call.",
        input_model=SetOutcomeInput,
        output_model=SetOutcomeOutput,
        permission_action="call.set_outcome",
        risk=ToolRisk.LOW,
        idempotent=True,
        timeout_seconds=_DB_TIMEOUT_SECONDS,
        handler=_set_outcome,
    )
)
TOOL_REGISTRY.register(
    ToolDefinition(
        tool_id="call.create_follow_up",
        name="call.create_follow_up",
        description="Create a follow-up action resulting from the current phone call.",
        input_model=CreateFollowUpInput,
        output_model=CreateFollowUpOutput,
        permission_action="call.create_follow_up",
        risk=ToolRisk.LOW,
        idempotent=False,
        timeout_seconds=_DB_TIMEOUT_SECONDS,
        handler=_create_follow_up,
    )
)


# -- Phase 2.10: workflow.advance ---------------------------------------------

#: Generous relative to a single DB round trip: one `workflow.advance` call
#: may walk several steps, one of which may itself be a nested Tool Gateway
#: call with its own (typically 10s) timeout -- see
#: `voiceagent.workflows.executor`'s own module docstring for why the loop
#: itself has no separate timeout: this `ToolDefinition.timeout_seconds`,
#: enforced by `voiceagent.tools.gateway.ToolGateway._run_handler()`, is the
#: one bound that covers the whole run.
_WORKFLOW_ADVANCE_TIMEOUT_SECONDS = 45.0


class WorkflowAdvanceInput(StrictToolModel):
    """No arguments: the workflow to advance is always the one published on
    *this* call's own `AgentVersion` -- the model can never name, construct,
    or select a different one (brief LLM BOUNDARY)."""


class WorkflowAdvanceOutput(StrictToolModel):
    status: Literal["completed", "failed", "cancelled", "not_configured", "running"]
    steps_executed: int
    final_step_id: str | None = None
    failure_reason: str | None = None


def _require_workflow_context(
    ctx: ToolExecutionContext,
) -> tuple[DatabaseBoundary, TenantContext, AgentVersion, ToolGateway, str]:
    """`workflow.advance` is only ever reachable through `voiceagent.tools
    .gateway.ToolGateway.execute()`, which always populates `db`/
    `tenant_context`/`agent_version`/`tool_gateway`/
    `system_service_account_name` (see `ToolExecutionContext`'s own
    docstring). `None` here would be a caller bug, not a real, reachable
    runtime state -- raised as a normalized `ToolExecutionError`, the same
    as `_require_db_context()` above."""
    if (
        ctx.db is None
        or ctx.tenant_context is None
        or ctx.agent_version is None
        or ctx.tool_gateway is None
        or ctx.system_service_account_name is None
    ):
        raise ToolExecutionError("internal_error", "tool execution context is incomplete")
    return (
        ctx.db,
        ctx.tenant_context,
        ctx.agent_version,
        ctx.tool_gateway,
        ctx.system_service_account_name,
    )


async def _workflow_advance(
    ctx: ToolExecutionContext, _input: StrictToolModel
) -> dict[str, object]:
    db, tenant_context, agent_version, tool_gateway, system_service_account_name = (
        _require_workflow_context(ctx)
    )
    result = await run_workflow(
        db=db,
        context=tenant_context,
        call_session_id=ctx.call_session_id,
        agent_version=agent_version,
        tool_gateway=tool_gateway,
        call_ref=ctx.call_ref,
        telephony=ctx.telephony,
        system_service_account_name=system_service_account_name,
    )
    if result.error_code is not None:
        # Only a genuine invocation-level problem (no workflow configured,
        # one already running, or a corrupted definition) raises here --
        # a workflow that ran and legitimately failed a step is reported as
        # a normal `status="failed"` value below (ADR-0003 point 8:
        # "failures are values at the model boundary"), never an exception.
        raise ToolExecutionError(result.error_code, "the workflow could not be advanced")
    return {
        "status": result.status,
        "steps_executed": result.steps_executed,
        "final_step_id": result.final_step_id,
        "failure_reason": result.failure_reason,
    }


# -- registration ------------------------------------------------------------

TOOL_REGISTRY.register(
    ToolDefinition(
        tool_id=WORKFLOW_TOOL_ID,
        name=WORKFLOW_TOOL_ID,
        description="Advance the current call's configured workflow, if the agent has one.",
        input_model=WorkflowAdvanceInput,
        output_model=WorkflowAdvanceOutput,
        permission_action=WORKFLOW_TOOL_ID,
        risk=ToolRisk.LOW,
        idempotent=True,
        timeout_seconds=_WORKFLOW_ADVANCE_TIMEOUT_SECONDS,
        handler=_workflow_advance,
    )
)


# -- Phase 2.11: knowledge.search ---------------------------------------------

_KNOWLEDGE_SEARCH_TOOL_ID = "knowledge.search"


class KnowledgeSearchInput(StrictToolModel):
    """The model supplies a query and nothing else. It may never supply a
    `tenant_id`, a result limit above `voiceagent.knowledge.retrieval
    .MAX_RESULT_LIMIT`, a source/table name, or any filter shape -- the
    server always derives tenant and agent/version context from `ctx`, and
    always clamps the result count itself (brief LLM CONTEXT BOUNDARY)."""

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)


class KnowledgeSearchResultRef(StrictToolModel):
    item_id: uuid.UUID
    source_id: uuid.UUID
    title: str
    snippet: str


class KnowledgeSearchOutput(StrictToolModel):
    results: list[KnowledgeSearchResultRef]


def _require_knowledge_context(
    ctx: ToolExecutionContext,
) -> tuple[DatabaseBoundary, TenantContext, AgentVersion]:
    """Identical shape to `_require_workflow_context()` above -- `None` here
    would be a caller bug (`knowledge.search` is only ever reachable through
    `ToolGateway.execute()`), not a real, reachable runtime state."""
    if ctx.db is None or ctx.tenant_context is None or ctx.agent_version is None:
        raise ToolExecutionError("internal_error", "tool execution context is incomplete")
    return ctx.db, ctx.tenant_context, ctx.agent_version


async def _knowledge_search(
    ctx: ToolExecutionContext, tool_input: StrictToolModel
) -> dict[str, object]:
    db, tenant_context, agent_version = _require_knowledge_context(ctx)
    query = cast(KnowledgeSearchInput, tool_input).query
    try:
        results = await db.run(
            search_items, tenant_context, agent_version, query=query, limit=MAX_RESULT_LIMIT
        )
    except InvalidKnowledgeQueryError as exc:
        raise ToolExecutionError("invalid_query", "the search query is invalid") from exc
    return {
        "results": [
            {
                "item_id": str(result.item_id),
                "source_id": str(result.source_id),
                "title": result.title,
                "snippet": result.snippet,
            }
            for result in results
        ]
    }


# -- registration ------------------------------------------------------------

TOOL_REGISTRY.register(
    ToolDefinition(
        tool_id=_KNOWLEDGE_SEARCH_TOOL_ID,
        name=_KNOWLEDGE_SEARCH_TOOL_ID,
        description=(
            "Search this tenant's approved knowledge (business hours, pricing, "
            "policies, FAQs) for content relevant to a query."
        ),
        input_model=KnowledgeSearchInput,
        output_model=KnowledgeSearchOutput,
        permission_action=_KNOWLEDGE_SEARCH_TOOL_ID,
        risk=ToolRisk.LOW,
        idempotent=True,
        timeout_seconds=_DB_TIMEOUT_SECONDS,
        handler=_knowledge_search,
    )
)
