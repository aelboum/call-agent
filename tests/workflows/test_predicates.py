"""`voiceagent.workflows.predicates.evaluate_predicate()` -- pure, DB-free
(Phase 2.10)."""

from __future__ import annotations

import dataclasses

import pytest

from voiceagent.workflows.config import PREDICATES
from voiceagent.workflows.predicates import CallState, evaluate_predicate

_DEFAULT_STATE = CallState(
    contact_associated=False,
    outcome_exists=False,
    follow_up_exists=False,
    appointment_exists=False,
    had_transfer=False,
    had_hold=False,
    elapsed_seconds=None,
)


def _state(**overrides) -> CallState:
    return dataclasses.replace(_DEFAULT_STATE, **overrides)


@pytest.mark.parametrize(
    ("predicate", "field", "expected_when_true"),
    [
        ("contact_associated", "contact_associated", True),
        ("outcome_exists", "outcome_exists", True),
        ("transfer_occurred", "had_transfer", True),
        ("hold_occurred", "had_hold", True),
        ("follow_up_exists", "follow_up_exists", True),
        ("appointment_exists", "appointment_exists", True),
    ],
)
def test_direct_boolean_predicates(predicate, field, expected_when_true) -> None:
    assert evaluate_predicate(predicate, _state(**{field: True}), threshold_seconds=None) is True
    assert evaluate_predicate(predicate, _state(**{field: False}), threshold_seconds=None) is False


def test_negated_predicates() -> None:
    not_associated = "contact_not_associated"
    associated = _state(contact_associated=True)
    unassociated = _state(contact_associated=False)
    assert evaluate_predicate(not_associated, unassociated, threshold_seconds=None) is True
    assert evaluate_predicate(not_associated, associated, threshold_seconds=None) is False

    not_exists = "outcome_not_exists"
    has_outcome = _state(outcome_exists=True)
    no_outcome = _state(outcome_exists=False)
    assert evaluate_predicate(not_exists, no_outcome, threshold_seconds=None) is True
    assert evaluate_predicate(not_exists, has_outcome, threshold_seconds=None) is False


def test_duration_predicate_true_when_elapsed_meets_threshold() -> None:
    state = _state(elapsed_seconds=90.0)
    predicate = "call_duration_at_least_seconds"
    assert evaluate_predicate(predicate, state, threshold_seconds=60) is True


def test_duration_predicate_false_when_elapsed_below_threshold() -> None:
    state = _state(elapsed_seconds=10.0)
    predicate = "call_duration_at_least_seconds"
    assert evaluate_predicate(predicate, state, threshold_seconds=60) is False


def test_duration_predicate_false_when_call_never_started() -> None:
    state = _state(elapsed_seconds=None)
    predicate = "call_duration_at_least_seconds"
    assert evaluate_predicate(predicate, state, threshold_seconds=60) is False


def test_unsupported_predicate_raises() -> None:
    with pytest.raises(ValueError, match="unsupported predicate"):
        evaluate_predicate("always_true", _state(), threshold_seconds=None)


def test_every_predicate_constant_is_handled() -> None:
    """No predicate in the closed vocabulary falls through unhandled."""
    for predicate in PREDICATES:
        evaluate_predicate(predicate, _state(elapsed_seconds=0.0), threshold_seconds=0)
