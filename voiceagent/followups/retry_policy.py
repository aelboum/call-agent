"""Bounded execution/retry policy for `FollowUpAction` (Phase 2.9 brief
§7/§8).

Pure and DB-free, deliberately mirroring `voiceagent.followups.lifecycle`'s
own "simplest explicit mechanism, hermetically unit-tested on its own"
discipline -- there is no generic retry framework here, just the small set
of named constants and one pure backoff function `voiceagent.followups
.service` consults.

**`EXECUTABLE_TYPES`** -- Phase 2.9 brief §5: "Only implement execution
types that have a concrete product meaning today." Only `'appointment'` has
one (`voiceagent.calendars.service`); `'contact'`/`'manual_follow_up'` have
no concrete application service to execute, so they are never claimed by
`voiceagent.followups.service.claim_due_follow_up()` and are surfaced only
through the existing read/complete/cancel API for a human to act on
directly (see `docs/PHASE-2.9-STATUS.md` "Non-goals").

**Backoff** mirrors the exact exponential formula the pinned SaaS-OS's own
`infra.jobs.queue._with_retry_and_dead_letter()` already uses
(`retry_backoff_base_seconds * (2 ** (job_try - 1))`) -- the one existing
numeric retry convention in this dependency stack -- rather than inventing a
second shape. `BACKOFF_CAP_SECONDS` bounds it so a follow-up stuck failing
for a long time does not wait for a multi-day interval before its next
attempt.

**`LEASE_SECONDS`** is how long a claimed (`status='processing'`) row is
presumed to be genuinely in flight before `claim_due_follow_up()` is willing
to reclaim it as stale (brief §9's "lease/timeout model"). It is
deliberately generous relative to a single `calendar_service.get_event()`
call -- the lease exists for the crash case, not the happy path.
"""

from __future__ import annotations

__all__ = [
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "EXECUTABLE_TYPES",
    "FAILURE_REASONS",
    "LEASE_SECONDS",
    "MAX_ATTEMPTS",
    "next_attempt_delay_seconds",
]

#: Follow-up types `claim_due_follow_up()` is willing to claim and execute
#: automatically. Every other `FOLLOW_UP_TYPES` member remains a valid,
#: creatable follow-up -- it is simply never picked up by the background
#: worker (brief §5).
EXECUTABLE_TYPES = frozenset({"appointment"})

#: A follow-up that has failed `MAX_ATTEMPTS` times is never claimed again
#: (brief §8: "no infinite retry loop"). It remains `status='failed'`,
#: visible and reportable, forever -- `reprocess_follow_up()` is the one
#: deliberate, permissioned way past this ceiling (it does not raise it; it
#: still refuses once `attempt_count >= MAX_ATTEMPTS`).
MAX_ATTEMPTS = 5

#: How long a claim's lease lasts before `claim_due_follow_up()` treats the
#: row as abandoned and reclaims it (brief §9).
LEASE_SECONDS = 300.0

#: Exponential backoff base/cap, matching `infra.jobs.queue`'s own
#: `JobsConfig.retry_backoff_base_seconds` default shape (module docstring).
BACKOFF_BASE_SECONDS = 60.0
BACKOFF_CAP_SECONDS = 3600.0

#: The closed, bounded vocabulary for `FollowUpAction.failure_reason` (brief
#: §8: "Do NOT store sensitive exception traces or arbitrary provider
#: responses" -- a short, stable code, never a message). Enforced by both
#: `ck_follow_up_actions_failure_reason` (migration `0007`) and
#: `voiceagent.followups.service`.
FAILURE_REASONS = frozenset(
    {
        "calendar_event_not_found",
        "calendar_event_cancelled",
        "max_attempts_exceeded",
        "unexpected_error",
    }
)


def next_attempt_delay_seconds(attempt_count: int) -> float:
    """Seconds to wait before a `status='failed'` follow-up with
    `attempt_count` recorded attempts becomes eligible for another claim.
    `attempt_count` must be `>= 1` (the count already includes the attempt
    that just failed) -- deterministic and pure, so this is exhaustively
    unit-tested without a database.
    """
    if attempt_count < 1:
        raise ValueError(f"attempt_count must be >= 1, got {attempt_count}")
    delay = BACKOFF_BASE_SECONDS * (2 ** (attempt_count - 1))
    return min(delay, BACKOFF_CAP_SECONDS)
