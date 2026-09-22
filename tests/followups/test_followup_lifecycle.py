"""`voiceagent.followups.lifecycle` -- pure, DB-free (mirrors
`tests/calls/test_lifecycle.py`)."""

from __future__ import annotations

import pytest

from voiceagent.followups.lifecycle import (
    TERMINAL_STATUSES,
    VALID_STATUSES,
    is_valid_follow_up_transition,
)

_LEGAL = [
    ("pending", "completed"),
    ("pending", "cancelled"),
]


@pytest.mark.parametrize("current, target", _LEGAL)
def test_legal_transitions_are_valid(current: str, target: str) -> None:
    assert is_valid_follow_up_transition(current, target) is True


_ILLEGAL = [
    ("completed", "pending"),  # terminal cannot reopen
    ("cancelled", "pending"),
    ("completed", "cancelled"),  # terminal cannot become a different terminal
    ("cancelled", "completed"),
]


@pytest.mark.parametrize("current, target", _ILLEGAL)
def test_illegal_transitions_are_rejected(current: str, target: str) -> None:
    assert is_valid_follow_up_transition(current, target) is False


@pytest.mark.parametrize("status", sorted(VALID_STATUSES))
def test_every_status_is_a_valid_no_op_transition_to_itself(status: str) -> None:
    assert is_valid_follow_up_transition(status, status) is True


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_terminal_statuses_have_no_outgoing_transitions_except_to_themselves(status: str) -> None:
    others = VALID_STATUSES - {status}
    for other in others:
        assert is_valid_follow_up_transition(status, other) is False


def test_terminal_statuses_are_exactly_two() -> None:
    assert TERMINAL_STATUSES == {"completed", "cancelled"}


def test_valid_statuses_are_exactly_three() -> None:
    assert VALID_STATUSES == {"pending", "completed", "cancelled"}
