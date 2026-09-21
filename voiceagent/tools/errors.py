"""Domain errors for the Tool Gateway (Phase 2.4).

Every one of these is caught inside `voiceagent.tools.gateway.ToolGateway
.execute()` and normalized into a `ToolResult` -- none of them, and no
handler-level exception either, is ever allowed to reach the model or the
call task as a raw Python exception (ADR-0003 point 8: "failures are values
at the model boundary").
"""

from __future__ import annotations

__all__ = [
    "DuplicateToolIdError",
    "ToolError",
    "ToolExecutionError",
    "UnknownToolError",
]


class ToolError(Exception):
    """Base class for every Tool Gateway domain error."""


class UnknownToolError(ToolError):
    """`ToolCallRequested.name` does not resolve in `TOOL_REGISTRY` -- an
    unknown tool ID fails closed (Phase 2.4 brief section 4)."""

    def __init__(self, tool_id: str) -> None:
        super().__init__(f"unknown tool: {tool_id!r}")
        self.tool_id = tool_id


class DuplicateToolIdError(ToolError):
    """Two `ToolDefinition`s were registered under the same `tool_id`."""

    def __init__(self, tool_id: str) -> None:
        super().__init__(f"duplicate tool id: {tool_id!r}")
        self.tool_id = tool_id


class ToolExecutionError(ToolError):
    """A tool's handler failed for an application-level reason (e.g. the
    call already ended, a transport error) that the handler chose to
    normalize itself rather than let escape as a raw exception.

    `retryable` becomes `ToolResult.retryable` unchanged -- the handler, not
    the gateway, is in the best position to know whether the *same*
    arguments might succeed on a later attempt.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
