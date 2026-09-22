"""`voiceagent.calendars.service`'s pure validation helpers -- `validate_timezone()`
and `_require_valid_interval()` both raise (or don't) *before*
`tenant_scope()` is ever opened, so they are hermetically unit-tested here
directly, exactly like `voiceagent.calls.lifecycle`. The DB-touching paths
(`create_calendar`, `check_availability`, `create_event`, `cancel_event`) are
covered by `tests/integration/test_contacts_calendar_integration.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from voiceagent.calendars.errors import (
    InvalidIntervalError,
    InvalidTimezoneError,
    NaiveDatetimeError,
)
from voiceagent.calendars.service import _require_valid_interval, validate_timezone


@pytest.mark.parametrize(
    "timezone",
    ["UTC", "Europe/Amsterdam", "Africa/Casablanca", "America/New_York"],
)
def test_valid_iana_timezones_are_accepted(timezone: str) -> None:
    validate_timezone(timezone)  # must not raise


@pytest.mark.parametrize(
    "timezone",
    ["", "Not/A/Zone", "PST", "GMT+2", "Earth/Everywhere"],
)
def test_invalid_timezones_are_rejected(timezone: str) -> None:
    with pytest.raises(InvalidTimezoneError):
        validate_timezone(timezone)


def _aware(hour: int) -> datetime:
    return datetime(2026, 10, 1, hour, tzinfo=UTC)


def test_a_valid_aware_interval_is_accepted() -> None:
    _require_valid_interval(_aware(10), _aware(11))  # must not raise


def test_naive_start_is_rejected() -> None:
    with pytest.raises(NaiveDatetimeError):
        _require_valid_interval(datetime(2026, 10, 1, 10), _aware(11))


def test_naive_end_is_rejected() -> None:
    with pytest.raises(NaiveDatetimeError):
        _require_valid_interval(_aware(10), datetime(2026, 10, 1, 11))


def test_naive_datetime_is_never_silently_treated_as_utc() -> None:
    """Brief §6: a naive datetime must be rejected outright, never assumed
    to already be UTC -- even when, coincidentally, it would compare
    correctly if it were."""
    with pytest.raises(NaiveDatetimeError):
        _require_valid_interval(datetime(2026, 10, 1, 10), datetime(2026, 10, 1, 11))


def test_start_not_before_end_is_rejected() -> None:
    with pytest.raises(InvalidIntervalError):
        _require_valid_interval(_aware(11), _aware(10))


def test_start_equal_to_end_is_rejected() -> None:
    with pytest.raises(InvalidIntervalError):
        _require_valid_interval(_aware(10), _aware(10))


def test_different_timezone_offsets_still_compare_correctly() -> None:
    """Two aware datetimes in different offsets that represent a valid
    ordering must be accepted -- comparison is by instant, not by wall-clock
    string."""
    start = datetime(2026, 10, 1, 10, tzinfo=UTC)
    end = (start + timedelta(hours=1)).astimezone(UTC).replace(tzinfo=UTC)
    _require_valid_interval(start, end)  # must not raise
