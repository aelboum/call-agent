"""Domain errors for `voiceagent.call_intelligence` (Phase 2.12)."""

from __future__ import annotations

__all__ = [
    "CallAiAnalysisError",
    "CallAiAnalysisExecutionConflictError",
    "CallAiAnalysisInProgressError",
    "CallAiAnalysisNotFoundError",
    "CallNotEligibleForAnalysisError",
    "InvalidCallAiAnalysisFailureReasonError",
]


class CallAiAnalysisError(Exception):
    """Base class for every AI post-call analysis domain error."""


class CallAiAnalysisNotFoundError(CallAiAnalysisError):
    def __init__(self, call_session_id: object) -> None:
        super().__init__(f"no CallAiAnalysis for call session: {call_session_id}")


class CallNotEligibleForAnalysisError(CallAiAnalysisError):
    """The call has not yet reached a terminal `voiceagent.calls.lifecycle
    .TERMINAL_STATUSES` status -- this is *post*-call intelligence; there is
    nothing durable and final to analyze while a call is still live."""

    def __init__(self, call_session_id: object) -> None:
        super().__init__(f"call session {call_session_id} has not completed yet")


class CallAiAnalysisInProgressError(CallAiAnalysisError):
    """A rebuild was explicitly requested (`force_rebuild=True`) while an
    earlier version for this call is still `pending`/`processing` -- refused
    rather than queuing a second, concurrent analysis of the same call
    (mirrors `voiceagent.workflows.errors.WorkflowExecutionInProgressError`)."""

    def __init__(self, call_session_id: object) -> None:
        super().__init__(
            f"an AI analysis is already in progress for call session {call_session_id}"
        )


class CallAiAnalysisExecutionConflictError(CallAiAnalysisError):
    """The row no longer matches the caller's own `execution_id` (or is no
    longer `status='processing'`) when a completion/failure was recorded --
    the same "lost the race, do not overwrite the winner" shape as
    `voiceagent.followups.errors.FollowUpExecutionConflictError`."""

    def __init__(self, analysis_id: object) -> None:
        super().__init__(f"CallAiAnalysis {analysis_id} is no longer owned by this run")


class InvalidCallAiAnalysisFailureReasonError(CallAiAnalysisError):
    def __init__(self) -> None:
        super().__init__("invalid CallAiAnalysis failure reason")
