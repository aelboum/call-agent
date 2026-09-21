"""`voiceagent.tools.allowlist` (Phase 2.4 brief section 4): the AgentVersion
tool allowlist, read from the existing `AgentConfig.tools` field -- no new
model, no new table."""

from __future__ import annotations

import uuid

from voiceagent.agents.models import AgentVersion
from voiceagent.tools.allowlist import allowed_tool_ids, is_tool_allowed


def _agent_version(*, tools: list[dict[str, object]] | None = None) -> AgentVersion:
    return AgentVersion(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        version_number=1,
        status="published",
        config={"tools": tools if tools is not None else []},
        config_hash="0" * 64,
    )


def test_allowed_tool_executes() -> None:
    version = _agent_version(tools=[{"key": "call.hangup", "config": {}}])
    assert is_tool_allowed(version, "call.hangup") is True


def test_non_allowed_tool_is_rejected() -> None:
    version = _agent_version(tools=[{"key": "call.hangup", "config": {}}])
    assert is_tool_allowed(version, "call.transfer") is False


def test_no_tools_configured_allows_nothing() -> None:
    version = _agent_version(tools=None)
    assert is_tool_allowed(version, "call.hangup") is False
    assert allowed_tool_ids(version) == frozenset()


def test_allowed_tool_ids_reflects_every_bound_key() -> None:
    version = _agent_version(
        tools=[
            {"key": "call.hangup", "config": {}},
            {"key": "call.hold", "config": {"max_seconds": 60}},
        ]
    )
    assert allowed_tool_ids(version) == frozenset({"call.hangup", "call.hold"})
