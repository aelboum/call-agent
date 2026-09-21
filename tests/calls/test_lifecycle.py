"""`voiceagent.calls.lifecycle` -- pure, DB-free (Phase 2.1 brief §14:
"use the simplest explicit state transition mechanism")."""

from __future__ import annotations

import pytest

from voiceagent.calls.lifecycle import TERMINAL_STATUSES, VALID_STATUSES, is_valid_transition

_LEGAL = [
    ("initiated", "ringing"),
    ("initiated", "answered"),
    ("initiated", "failed"),
    ("initiated", "interrupted"),
    ("ringing", "answered"),
    ("ringing", "failed"),
    ("answered", "in_progress"),
    ("answered", "completed"),
    ("in_progress", "completed"),
    ("in_progress", "failed"),
    ("in_progress", "interrupted"),
]


@pytest.mark.parametrize("current, target", _LEGAL)
def test_legal_transitions_are_valid(current: str, target: str) -> None:
    assert is_valid_transition(current, target) is True


_ILLEGAL = [
    ("initiated", "in_progress"),  # cannot skip straight to in_progress
    ("ringing", "initiated"),  # no going backward
    ("completed", "in_progress"),  # terminal cannot reopen
    ("failed", "completed"),  # terminal cannot become a different terminal
    ("interrupted", "answered"),
]


@pytest.mark.parametrize("current, target", _ILLEGAL)
def test_illegal_transitions_are_rejected(current: str, target: str) -> None:
    assert is_valid_transition(current, target) is False


@pytest.mark.parametrize("status", sorted(VALID_STATUSES))
def test_every_status_is_a_valid_no_op_transition_to_itself(status: str) -> None:
    """Duplicate lifecycle events (Phase 2.0 report §16/§17) must not
    corrupt state -- redelivering the event that produced the current status
    is a no-op, including for a terminal status."""
    assert is_valid_transition(status, status) is True


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_terminal_statuses_have_no_outgoing_transitions_except_to_themselves(status: str) -> None:
    others = VALID_STATUSES - {status}
    for other in others:
        assert is_valid_transition(status, other) is False


def test_terminal_statuses_are_exactly_three() -> None:
    assert TERMINAL_STATUSES == {"completed", "failed", "interrupted"}
