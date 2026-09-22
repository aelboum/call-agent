"""Bounded execution/retry policy for `CallAiAnalysis` (Phase 2.12).

Pure and DB-free, deliberately mirroring `voiceagent.followups.retry_policy`
's own "simplest explicit mechanism, hermetically unit-tested on its own"
discipline and its exact exponential-backoff formula (itself matching the
pinned SaaS-OS's own `infra.jobs.queue._with_retry_and_dead_letter()`
convention) -- no second retry shape is invented for this domain.

**`LEASE_SECONDS`** is generous relative to one external AI provider round
trip (bounded by `CallIntelligenceSettings.timeout_seconds`, typically well
under a minute) -- the lease exists for the crash/cancellation case (brief
WORKER: "cancellation handling", "no lease/reclaim... held across an
external provider call"), not the happy path.
"""

from __future__ import annotations

__all__ = [
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "LEASE_SECONDS",
    "MAX_ATTEMPTS",
    "next_attempt_delay_seconds",
]

#: A `status='failed'` analysis that has already been attempted this many
#: times is never claimed again (brief: "bounded attempts", "no infinite
#: retry loop"). It remains visible and reportable, forever --
#: `voiceagent.call_intelligence.service.request_analysis(force_rebuild=True)`
#: is the one deliberate, permissioned way past this ceiling: it starts a
#: fresh *version* (its own `attempt_count` back at zero), never raises this
#: one row's own ceiling.
MAX_ATTEMPTS = 3

#: How long a claim's lease lasts before `claim_pending_analysis()` treats
#: the row as abandoned (a crashed worker, or a cancelled provider call) and
#: reclaims it (brief: "stale-processing handling", "cancellation
#: handling").
LEASE_SECONDS = 120.0

#: Exponential backoff base/cap -- same shape as
#: `voiceagent.followups.retry_policy`, smaller numbers: a post-call
#: analysis is not time-critical the way a scheduled appointment follow-up
#: is, but a failing provider still should not be hammered every tick.
BACKOFF_BASE_SECONDS = 30.0
BACKOFF_CAP_SECONDS = 900.0


def next_attempt_delay_seconds(attempt_count: int) -> float:
    """Seconds to wait before a `status='failed'` analysis with
    `attempt_count` recorded attempts becomes eligible for another claim.
    `attempt_count` must be `>= 1` (the count already includes the attempt
    that just failed)."""
    if attempt_count < 1:
        raise ValueError(f"attempt_count must be >= 1, got {attempt_count}")
    delay = BACKOFF_BASE_SECONDS * (2 ** (attempt_count - 1))
    return min(delay, BACKOFF_CAP_SECONDS)
