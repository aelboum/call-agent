"""Domain errors for the Agent aggregate.

Raised by `voiceagent.agents.service` and translated to `HTTPException`s at
the API boundary (`voiceagent.api.v1.agents`) -- never allowed to reach a
client as a raw exception (Phase 2.1 brief §24: "no raw DB exception
leakage"). None of these messages carries data from another tenant's row; a
foreign or nonexistent id is always `AgentNotFoundError`/
`AgentVersionNotFoundError`, the same non-enumerating shape SaaS-OS's own
reference consumer uses.
"""

from __future__ import annotations

__all__ = [
    "AgentError",
    "AgentNotFoundError",
    "AgentVersionNotDraftError",
    "AgentVersionNotFoundError",
    "AgentVersionNotPublishedError",
    "InvalidAgentConfigError",
]


class AgentError(Exception):
    """Base class for every Agent-aggregate domain error."""


class AgentNotFoundError(AgentError):
    def __init__(self, agent_id: object) -> None:
        super().__init__(f"Agent not found: {agent_id}")


class AgentVersionNotFoundError(AgentError):
    def __init__(self, version_id: object) -> None:
        super().__init__(f"AgentVersion not found: {version_id}")


class AgentVersionNotDraftError(AgentError):
    """Publishing (or editing) requires a `draft` version; raised when the
    target row is already `published` or `archived`."""

    def __init__(self, version_id: object, status: str) -> None:
        super().__init__(f"AgentVersion {version_id} is {status!r}, not 'draft'")


class AgentVersionNotPublishedError(AgentError):
    """Archiving requires a `published` version (ADR-0004's own diagram
    names exactly one transition out of `published`: to `archived`)."""

    def __init__(self, version_id: object, status: str) -> None:
        super().__init__(f"AgentVersion {version_id} is {status!r}, not 'published'")


class InvalidAgentConfigError(AgentError):
    """The submitted `config` payload does not match the documented shape
    (`voiceagent.agents.config.AgentConfig`)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
