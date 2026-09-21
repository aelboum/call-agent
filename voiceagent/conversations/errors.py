"""Domain errors for conversation turns."""

from __future__ import annotations

__all__ = ["ConversationError", "InvalidConversationTurnError"]


class ConversationError(Exception):
    """Base class for every conversation-turn domain error."""


class InvalidConversationTurnError(ConversationError):
    def __init__(self, role: str) -> None:
        super().__init__(f"invalid conversation turn role: {role!r}")
