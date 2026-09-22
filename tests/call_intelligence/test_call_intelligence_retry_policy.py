"""`voiceagent.call_intelligence.retry_policy` (Phase 2.12) -- pure, DB-free,
exactly like `voiceagent.followups.retry_policy`'s own test suite."""

from __future__ import annotations

import pytest

from voiceagent.call_intelligence.retry_policy import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    MAX_ATTEMPTS,
    next_attempt_delay_seconds,
)


def test_first_attempt_delay_is_the_base() -> None:
    assert next_attempt_delay_seconds(1) == BACKOFF_BASE_SECONDS


def test_delay_doubles_each_attempt() -> None:
    assert next_attempt_delay_seconds(2) == BACKOFF_BASE_SECONDS * 2
    assert next_attempt_delay_seconds(3) == BACKOFF_BASE_SECONDS * 4


def test_delay_is_capped() -> None:
    assert next_attempt_delay_seconds(100) == BACKOFF_CAP_SECONDS


def test_zero_or_negative_attempt_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="attempt_count must be >= 1"):
        next_attempt_delay_seconds(0)
    with pytest.raises(ValueError):
        next_attempt_delay_seconds(-1)


def test_max_attempts_is_a_small_positive_bound() -> None:
    assert 0 < MAX_ATTEMPTS <= 10
