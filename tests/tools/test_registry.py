"""`voiceagent.tools.registry.ToolRegistry` (Phase 2.4 brief section 14)."""

from __future__ import annotations

import pytest

from voiceagent.tools.definitions import (
    StrictToolModel,
    ToolDefinition,
    ToolExecutionContext,
    ToolRisk,
)
from voiceagent.tools.errors import DuplicateToolIdError, UnknownToolError
from voiceagent.tools.registry import TOOL_REGISTRY, ToolRegistry


class _Input(StrictToolModel):
    pass


class _Output(StrictToolModel):
    ok: bool = True


async def _noop_handler(ctx: ToolExecutionContext, _input: StrictToolModel) -> dict[str, object]:
    return {"ok": True}


def _definition(tool_id: str = "test.tool") -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        name=tool_id,
        description="a test tool",
        input_model=_Input,
        output_model=_Output,
        permission_action=tool_id,
        risk=ToolRisk.LOW,
        idempotent=True,
        timeout_seconds=1.0,
        handler=_noop_handler,
    )


def test_known_tool_resolves() -> None:
    registry = ToolRegistry()
    definition = _definition()
    registry.register(definition)
    assert registry.resolve("test.tool") is definition


def test_unknown_tool_fails_closed() -> None:
    registry = ToolRegistry()
    with pytest.raises(UnknownToolError):
        registry.resolve("does.not.exist")


def test_duplicate_tool_id_is_rejected() -> None:
    registry = ToolRegistry()
    registry.register(_definition())
    with pytest.raises(DuplicateToolIdError):
        registry.register(_definition())


def test_known_tool_ids_is_sorted_and_reflects_registrations() -> None:
    registry = ToolRegistry()
    registry.register(_definition("b.tool"))
    registry.register(_definition("a.tool"))
    assert registry.known_tool_ids() == ("a.tool", "b.tool")


def test_contains_reflects_registration_state() -> None:
    registry = ToolRegistry()
    assert "test.tool" not in registry
    registry.register(_definition())
    assert "test.tool" in registry


def test_tool_definition_is_immutable() -> None:
    definition = _definition()
    with pytest.raises(Exception):  # noqa: B017, PT011 -- frozen dataclass raises FrozenInstanceError
        definition.tool_id = "changed"  # type: ignore[misc]


def test_the_process_wide_registry_holds_exactly_the_eight_built_in_tools() -> None:
    """Importing `voiceagent.tools.handlers` (transitively, via
    `voiceagent.tools.permissions` or any earlier test in the same process)
    populates `TOOL_REGISTRY` with exactly Phase 2.4's four call-control
    tools plus Phase 2.6's four Contact/Calendar tools -- no more, no fewer
    (brief section 6's "deliberately small initial tool set")."""
    import voiceagent.tools.handlers  # noqa: F401 -- import for its registration side effect

    assert TOOL_REGISTRY.known_tool_ids() == (
        "calendar.cancel_appointment",
        "calendar.check_availability",
        "calendar.create_appointment",
        "call.hangup",
        "call.hold",
        "call.resume",
        "call.transfer",
        "contact.lookup_by_phone",
    )


def test_input_schema_and_output_schema_are_derived_from_the_typed_models() -> None:
    import voiceagent.tools.handlers as handlers

    definition = TOOL_REGISTRY.resolve("call.transfer")
    assert definition.input_model is handlers.TransferInput
    schema = definition.input_schema
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert "destination_e164" in properties
    assert schema["required"] == ["destination_e164"]
