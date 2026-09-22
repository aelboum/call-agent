"""`voiceagent.call_analysis.service._tool_name()` -- the one piece of this
module's metric logic that is pure and DB-free, so it is hermetically unit-
tested here directly. Every other rule (turn/follow-up counting, duration,
contact association, idempotent rebuild, tenant isolation) requires a real
persisted `CallSession`/`ConversationTurn`/`CallOutcome`/`FollowUpAction`
and is covered by
`tests/integration/test_call_analysis_integration.py`.
"""

from __future__ import annotations

import uuid

from voiceagent.call_analysis.service import _HOLD_TOOL_ID, _TRANSFER_TOOL_ID, _tool_name
from voiceagent.conversations.models import ConversationTurn


def _turn(*, role: str, tool_payload: dict[str, object] | None) -> ConversationTurn:
    return ConversationTurn(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        call_session_id=uuid.uuid4(),
        event_id="e1",
        sequence=0,
        role=role,
        content=None,
        tool_payload=tool_payload,
    )


def test_tool_name_reads_the_name_field_of_a_tool_call_payload() -> None:
    turn = _turn(role="tool_call", tool_payload={"name": "call.transfer", "arguments": {}})
    assert _tool_name(turn) == "call.transfer"


def test_tool_name_is_none_when_there_is_no_payload() -> None:
    turn = _turn(role="user", tool_payload=None)
    assert _tool_name(turn) is None


def test_tool_name_is_none_when_the_payload_has_no_name_field() -> None:
    """`tool_result` turns carry `{"value", "error_code"}`, never `"name"`."""
    turn = _turn(role="tool_result", tool_payload={"value": {"ok": True}, "error_code": None})
    assert _tool_name(turn) is None


def test_transfer_and_hold_tool_ids_match_the_registered_tool_ids() -> None:
    """Brief §10: `had_transfer`/`had_hold` must key off the exact tool id
    `voiceagent.tools.handlers` registers -- verified directly against the
    real registry, not just re-asserted as a literal here."""
    import voiceagent.tools.handlers  # noqa: F401 -- registers the built-in tools
    from voiceagent.tools.registry import TOOL_REGISTRY

    assert _TRANSFER_TOOL_ID in TOOL_REGISTRY
    assert _HOLD_TOOL_ID in TOOL_REGISTRY
    assert _TRANSFER_TOOL_ID == "call.transfer"
    assert _HOLD_TOOL_ID == "call.hold"
