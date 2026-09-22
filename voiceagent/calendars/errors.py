"""Domain errors for `Calendar`/`CalendarEvent` (Phase 2.6)."""

from __future__ import annotations

__all__ = [
    "CalendarError",
    "CalendarEventConflictError",
    "CalendarEventNotFoundError",
    "CalendarNotFoundError",
    "InvalidIntervalError",
    "InvalidTimezoneError",
    "NaiveDatetimeError",
]


class CalendarError(Exception):
    """Base class for every Calendar/CalendarEvent domain error."""


class CalendarNotFoundError(CalendarError):
    def __init__(self, calendar_id: object) -> None:
        super().__init__(f"Calendar not found: {calendar_id}")


class CalendarEventNotFoundError(CalendarError):
    def __init__(self, event_id: object) -> None:
        super().__init__(f"CalendarEvent not found: {event_id}")


class InvalidTimezoneError(CalendarError):
    """`timezone` is not a recognized IANA timezone identifier. Deliberately
    carries no echo of the rejected value -- the caller already has it."""

    def __init__(self) -> None:
        super().__init__("Invalid IANA timezone identifier.")


class NaiveDatetimeError(CalendarError):
    """A timezone-naive datetime reached the service boundary. Never
    silently interpreted as UTC or as the calendar's own timezone (brief
    §6)."""

    def __init__(self) -> None:
        super().__init__("Datetime must be timezone-aware.")


class InvalidIntervalError(CalendarError):
    """`start_at` is not strictly before `end_at`."""

    def __init__(self) -> None:
        super().__init__("start_at must be before end_at.")


class CalendarEventConflictError(CalendarError):
    """The requested interval overlaps an existing scheduled event on the
    same calendar (brief §7)."""

    def __init__(self) -> None:
        super().__init__("Requested interval conflicts with an existing appointment.")
