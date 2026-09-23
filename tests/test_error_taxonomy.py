"""`voiceagent.error_taxonomy.categorize_exception()` (Phase 2.14 brief
section 9): every recognized exception maps onto its documented bounded
category, and an unrecognized one falls back to `"internal"` -- never
raises."""

from __future__ import annotations

import asyncio

import pytest

from voiceagent.calls.errors import (
    CallSessionAlreadyOwnedError,
    CallSessionOwnershipMismatchError,
    InvalidCallSessionTransitionError,
)
from voiceagent.error_taxonomy import categorize_exception
from voiceagent.providers.engines.contracts import EngineErrorCode, EngineException
from voiceagent.runtime.errors import (
    CallCancelledError,
    DataAuthorizationDeniedError,
    NoRuntimeCapacityError,
)
from voiceagent.telephony.contracts import TransportError, UnsupportedFormatError
from voiceagent.tools.errors import UnknownToolError
from voiceagent.workflows.errors import WorkflowNotConfiguredError


def test_cancelled_error_categorizes_as_cancellation() -> None:
    assert categorize_exception(asyncio.CancelledError()) == "cancellation"


def test_call_cancelled_error_categorizes_as_cancellation() -> None:
    assert categorize_exception(CallCancelledError("call-1", "hangup")) == "cancellation"


def test_timeout_error_categorizes_as_timeout() -> None:
    assert categorize_exception(TimeoutError()) == "timeout"


@pytest.mark.parametrize("code", list(EngineErrorCode))
def test_engine_exception_reuses_its_own_error_code_verbatim(code: EngineErrorCode) -> None:
    """Brief section 9: reuse `EngineErrorCode`, do not redesign it into a
    second, differently-spelled taxonomy."""
    assert categorize_exception(EngineException(code, "boom")) == code.value


def test_data_authorization_denied_categorizes_as_privacy_denied() -> None:
    assert (
        categorize_exception(DataAuthorizationDeniedError("call-1", "policy_denied"))
        == "privacy_denied"
    )


def test_call_session_ownership_mismatch_categorizes_as_tenant_isolation() -> None:
    exc = CallSessionOwnershipMismatchError(
        "call-1", expected_runtime_instance_id="a", actual_runtime_instance_id="b"
    )
    assert categorize_exception(exc) == "tenant_isolation"


def test_call_session_already_owned_falls_back_to_internal() -> None:
    """Not one of this module's explicitly recognized types -- the bounded
    catch-all applies, same as any other unrecognized exception."""
    assert categorize_exception(CallSessionAlreadyOwnedError("call-1", "runtime-a")) == "internal"


def test_invalid_call_session_transition_categorizes_as_validation() -> None:
    exc = InvalidCallSessionTransitionError("call-1", "completed", "in_progress")
    assert categorize_exception(exc) == "validation"


def test_no_runtime_capacity_falls_back_to_internal() -> None:
    assert categorize_exception(NoRuntimeCapacityError()) == "internal"


def test_telephony_error_categorizes_as_telephony() -> None:
    assert categorize_exception(TransportError("esl down")) == "telephony"
    assert categorize_exception(UnsupportedFormatError("bad codec")) == "telephony"


def test_tool_error_categorizes_as_tool() -> None:
    assert categorize_exception(UnknownToolError("not_a_tool")) == "tool"


def test_workflow_error_categorizes_as_workflow() -> None:
    assert categorize_exception(WorkflowNotConfiguredError("agent-1")) == "workflow"


def test_unrecognized_exception_falls_back_to_internal() -> None:
    assert categorize_exception(ValueError("something else entirely")) == "internal"


def test_never_raises_for_a_bare_exception_instance() -> None:
    assert categorize_exception(Exception()) == "internal"
