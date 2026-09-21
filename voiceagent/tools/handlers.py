"""Phase 2.4's four built-in call-control tools.

Every handler is `Tool -> application capability -> TelephonyProvider`
(brief section 13) -- none of them imports FreeSWITCH/ESL, `mod_audio_stream`,
or anything from `voiceagent.telephony.freeswitch`; each one calls exactly
one `voiceagent.telephony.contracts.TelephonyProvider` method, already
resolved onto this call's own `CallRef` by `voiceagent.tools.gateway
.ToolGateway` before the handler ever runs (never a destination the model
could redirect at a *different* call). `TelephonyProvider` already has
`hangup()`/`transfer()`/`hold()`/`unhold()` (Phase 2.2) -- no contract
change was needed to add these four tools.

`call.transfer`'s `destination_e164` is validated by `TransferInput`'s own
`Field(pattern=...)` before the handler -- or the ESL command string
`FreeSwitchTelephonyProvider.transfer()` builds by direct interpolation --
ever sees it (`voiceagent/telephony/freeswitch/provider.py`: `f"bgapi
originate sofia/gateway/default/{destination}"`). This is the concrete
defense the brief's "never accept arbitrary executable destinations" asks
for: a `TransferInput` that fails E.164 validation never reaches that
f-string.
"""

from __future__ import annotations

from typing import Literal, cast

from pydantic import Field

from voiceagent.telephony.contracts import HangupCause, TelephonyError
from voiceagent.tools.definitions import (
    StrictToolModel,
    ToolDefinition,
    ToolExecutionContext,
    ToolRisk,
)
from voiceagent.tools.errors import ToolExecutionError
from voiceagent.tools.registry import TOOL_REGISTRY

__all__ = [
    "HangupInput",
    "HangupOutput",
    "HoldInput",
    "HoldOutput",
    "ResumeInput",
    "ResumeOutput",
    "TransferInput",
    "TransferOutput",
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
