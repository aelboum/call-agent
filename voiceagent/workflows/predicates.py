"""Pure evaluation of the closed `voiceagent.workflows.config.PREDICATES`
vocabulary against already-fetched call state (Phase 2.10).

Deliberately DB-free -- `voiceagent.workflows.service.evaluate_condition()`
fetches the handful of rows a predicate might need (mirroring
`voiceagent.call_analysis.service.build_call_analysis()`'s own read set:
`CallSession`, `CallOutcome`, `FollowUpAction`, `ConversationTurn`) and hands
this module plain facts, never a session or a query. This is the one place
"which predicates exist and what each one means" is decided; nothing here
constructs, parses, or evaluates a general boolean expression.
"""

from __future__ import annotations

from dataclasses import dataclass

from voiceagent.workflows.config import PREDICATES

__all__ = ["CallState", "evaluate_predicate"]


@dataclass(frozen=True, slots=True)
class CallState:
    """The small, closed set of facts a predicate can be evaluated against
    -- every field here is deterministically derived from already-persisted
    rows, exactly the same authoritative sources `voiceagent.call_analysis
    .service.build_call_analysis()` already documents (`had_transfer`/
    `had_hold` from `ConversationTurn`, `contact_associated` from
    `CallSession.contact_id`, and so on)."""

    contact_associated: bool
    outcome_exists: bool
    follow_up_exists: bool
    appointment_exists: bool
    had_transfer: bool
    had_hold: bool
    #: `None` if the call has not started yet (never claimed answered) --
    #: `call_duration_at_least_seconds` is `False` in that case, never an
    #: error.
    elapsed_seconds: float | None


def evaluate_predicate(predicate: str, state: CallState, *, threshold_seconds: int | None) -> bool:
    """`predicate` must be a member of `PREDICATES` -- callers (`voiceagent
    .workflows.config.WorkflowConditionBranch` at parse time, and this
    function itself as a defense-in-depth guard) never let an unrecognized
    or unsupported predicate reach here."""
    if predicate not in PREDICATES:
        raise ValueError(f"unsupported predicate: {predicate!r}")

    if predicate == "contact_associated":
        return state.contact_associated
    if predicate == "contact_not_associated":
        return not state.contact_associated
    if predicate == "outcome_exists":
        return state.outcome_exists
    if predicate == "outcome_not_exists":
        return not state.outcome_exists
    if predicate == "transfer_occurred":
        return state.had_transfer
    if predicate == "hold_occurred":
        return state.had_hold
    if predicate == "follow_up_exists":
        return state.follow_up_exists
    if predicate == "appointment_exists":
        return state.appointment_exists
    # predicate == "call_duration_at_least_seconds" -- the only member left.
    if threshold_seconds is None or state.elapsed_seconds is None:
        return False
    return state.elapsed_seconds >= threshold_seconds
