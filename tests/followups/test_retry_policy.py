"""`voiceagent.followups.retry_policy` -- pure, DB-free."""

from __future__ import annotations

import pytest

from voiceagent.followups.retry_policy import (
    BACKOFF_CAP_SECONDS,
    EXECUTABLE_TYPES,
    FAILURE_REASONS,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    next_attempt_delay_seconds,
)


def test_executable_types_is_exactly_appointment() -> None:
    """Phase 2.9 brief §5: only a type with a concrete application service
    is auto-executed."""
    assert EXECUTABLE_TYPES == {"appointment"}


def test_max_attempts_is_positive_and_bounded() -> None:
    assert 0 < MAX_ATTEMPTS < 100


def test_lease_seconds_is_positive() -> None:
    assert LEASE_SECONDS > 0


def test_failure_reasons_is_a_closed_four_value_vocabulary() -> None:
    assert FAILURE_REASONS == {
        "calendar_event_not_found",
        "calendar_event_cancelled",
        "max_attempts_exceeded",
        "unexpected_error",
    }


def test_next_attempt_delay_rejects_non_positive_attempt_count() -> None:
    with pytest.raises(ValueError, match="attempt_count"):
        next_attempt_delay_seconds(0)
    with pytest.raises(ValueError, match="attempt_count"):
        next_attempt_delay_seconds(-1)


def test_next_attempt_delay_grows_with_attempt_count() -> None:
    delays = [next_attempt_delay_seconds(n) for n in range(1, MAX_ATTEMPTS + 1)]
    assert all(earlier <= later for earlier, later in zip(delays, delays[1:], strict=False))
    assert delays[-1] > delays[0]


def test_next_attempt_delay_is_deterministic() -> None:
    assert next_attempt_delay_seconds(3) == next_attempt_delay_seconds(3)


def test_next_attempt_delay_first_attempt_matches_backoff_base() -> None:
    from voiceagent.followups.retry_policy import BACKOFF_BASE_SECONDS

    assert next_attempt_delay_seconds(1) == BACKOFF_BASE_SECONDS


def test_next_attempt_delay_is_capped() -> None:
    huge_attempt_count = 50
    assert next_attempt_delay_seconds(huge_attempt_count) == BACKOFF_CAP_SECONDS
