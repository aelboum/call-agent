"""Domain errors for `CallAnalysis` (Phase 2.8)."""

from __future__ import annotations

__all__ = ["CallAnalysisError", "CallAnalysisNotFoundError"]


class CallAnalysisError(Exception):
    """Base class for every CallAnalysis domain error."""


class CallAnalysisNotFoundError(CallAnalysisError):
    def __init__(self, call_session_id: object) -> None:
        super().__init__(f"CallAnalysis not found for call session: {call_session_id}")
