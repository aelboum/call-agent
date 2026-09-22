"""Domain errors for `CallOutcome`/`FollowUpAction` (Phase 2.7)."""

from __future__ import annotations

__all__ = [
    "CallOutcomeAlreadyExistsError",
    "CallOutcomeError",
    "CallOutcomeNotFoundError",
    "FollowUpActionNotFoundError",
    "FollowUpAppointmentRequiresCalendarEventError",
    "FollowUpError",
    "FollowUpExecutionConflictError",
    "FollowUpInvalidRelationshipError",
    "FollowUpNotRetryableError",
    "InvalidFollowUpFailureReasonError",
    "InvalidFollowUpStatusError",
    "InvalidFollowUpTransitionError",
    "InvalidFollowUpTypeError",
    "InvalidOutcomeValueError",
]


class CallOutcomeError(Exception):
    """Base class for every CallOutcome domain error."""


class CallOutcomeNotFoundError(CallOutcomeError):
    def __init__(self, call_session_id: object) -> None:
        super().__init__(f"CallOutcome not found for call session: {call_session_id}")


class CallOutcomeAlreadyExistsError(CallOutcomeError):
    """Raised by `create_call_outcome()` -- a call has at most one current
    outcome (brief §6); use `update_call_outcome()`/`set_call_outcome()` to
    change it."""

    def __init__(self, call_session_id: object) -> None:
        super().__init__(f"CallOutcome already exists for call session: {call_session_id}")


class InvalidOutcomeValueError(CallOutcomeError):
    """`outcome` is not one of `voiceagent.followups.models.OUTCOME_VALUES`.
    Deliberately carries no echo of the rejected value -- the caller already
    has it."""

    def __init__(self) -> None:
        super().__init__("Invalid outcome value.")


class FollowUpError(Exception):
    """Base class for every FollowUpAction domain error."""


class FollowUpActionNotFoundError(FollowUpError):
    def __init__(self, follow_up_id: object) -> None:
        super().__init__(f"FollowUpAction not found: {follow_up_id}")


class InvalidFollowUpTypeError(FollowUpError):
    def __init__(self) -> None:
        super().__init__("Invalid follow-up type.")


class InvalidFollowUpTransitionError(FollowUpError):
    def __init__(self, follow_up_id: object, current: str, target: str) -> None:
        super().__init__(
            f"FollowUpAction {follow_up_id} cannot transition {current!r} -> {target!r}"
        )


class FollowUpAppointmentRequiresCalendarEventError(FollowUpError):
    """`type='appointment'` requires either an existing `calendar_event_id`
    or enough parameters (`calendar_id`, `start_at`, `end_at`) for
    `create_follow_up()` to create one via
    `voiceagent.calendars.service.create_event()` (brief §8's deterministic
    rule: reject outright, never a silent pending state)."""

    def __init__(self) -> None:
        super().__init__(
            "An appointment follow-up requires an existing calendar event or "
            "calendar_id/start_at/end_at to create one."
        )


class FollowUpInvalidRelationshipError(FollowUpError):
    """A non-appointment follow-up was given a `calendar_event_id` (brief
    §8: `calendar_event_id` is set if and only if `type == 'appointment'`)."""

    def __init__(self) -> None:
        super().__init__("calendar_event_id is only valid for an appointment follow-up.")


class FollowUpExecutionConflictError(FollowUpError):
    """Raised by `complete_follow_up_execution()`/`fail_follow_up_execution()`
    when the row is no longer `status='processing'` under the caller's own
    `execution_id` (Phase 2.9 brief §7) -- its lease already expired and a
    different claim reclaimed (or finished) it first. Never a corruption:
    the caller's own attempt simply lost the race and must not overwrite the
    winner's outcome."""

    def __init__(self, follow_up_id: object) -> None:
        super().__init__(f"FollowUpAction {follow_up_id} is no longer owned by this execution.")


class FollowUpNotRetryableError(FollowUpError):
    """Raised by `reprocess_follow_up()` (Phase 2.9 brief §12/§13) when the
    follow-up is not `status='failed'`, or has already exhausted
    `voiceagent.followups.retry_policy.MAX_ATTEMPTS` -- the dedicated
    administrative retry endpoint never raises the bounded-retry ceiling."""

    def __init__(self, follow_up_id: object) -> None:
        super().__init__(f"FollowUpAction {follow_up_id} is not retryable.")


class InvalidFollowUpFailureReasonError(FollowUpError):
    """`reason` is not one of `voiceagent.followups.retry_policy
    .FAILURE_REASONS` -- `fail_follow_up_execution()`'s own closed
    vocabulary (brief §8: never a raw exception message)."""

    def __init__(self) -> None:
        super().__init__("Invalid follow-up failure reason.")


class InvalidFollowUpStatusError(FollowUpError):
    """`status` is not one of `voiceagent.followups.lifecycle
    .VALID_STATUSES` -- raised by `list_follow_ups_by_status()`."""

    def __init__(self) -> None:
        super().__init__("Invalid follow-up status.")
