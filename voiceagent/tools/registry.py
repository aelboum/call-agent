"""`ToolRegistry` -- tool ID -> `ToolDefinition`, the mechanism, not the
policy (Phase 2.4; mirrors `voiceagent.providers.registry.ProviderRegistry`'s
own split of "one small mechanism class" from "one module per policy area").

Unlike `ProviderRegistry.register()` (idempotent-by-name, a later
registration replaces an earlier one -- appropriate for a provider a test
substitutes), `ToolRegistry.register()` **raises on a duplicate `tool_id`**
(Phase 2.4 brief section 14: "duplicate tool IDs rejected"). A tool
definition is not something a test swaps out; two definitions claiming the
same ID is a programming error in this product's own code, caught at import
time, not a substitution point.
"""

from __future__ import annotations

from voiceagent.tools.definitions import ToolDefinition
from voiceagent.tools.errors import DuplicateToolIdError, UnknownToolError

__all__ = ["TOOL_REGISTRY", "ToolRegistry"]


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.tool_id in self._definitions:
            raise DuplicateToolIdError(definition.tool_id)
        self._definitions[definition.tool_id] = definition

    def resolve(self, tool_id: str) -> ToolDefinition:
        try:
            return self._definitions[tool_id]
        except KeyError:
            raise UnknownToolError(tool_id) from None

    def __contains__(self, tool_id: str) -> bool:
        return tool_id in self._definitions

    def known_tool_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))


#: The process-wide registry. `voiceagent.tools.handlers` populates it with
#: Phase 2.4's four built-in tools at import time (pure in-memory
#: registration -- no I/O, exactly like `voiceagent.providers.engines.factory
#: .REALTIME_PROVIDERS.register("fake", ...)` already does at import time).
TOOL_REGISTRY = ToolRegistry()
