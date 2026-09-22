"""`voiceagent.followups.service`'s pure, pre-`tenant_scope()` validation
paths -- exactly like `voiceagent.calendars.service`'s own
`NaiveDatetimeError`/`InvalidIntervalError` checks, these raise *before* any
database session opens, so they are hermetically unit-tested here directly.
The DB-touching paths (create/get/update/complete/cancel against a real
row) are covered by
`tests/integration/test_call_outcomes_followups_integration.py`.
"""

from __future__ import annotations

import uuid

import pytest

from voiceagent.followups.errors import (
    FollowUpInvalidRelationshipError,
    InvalidFollowUpTypeError,
    InvalidOutcomeValueError,
)
from voiceagent.followups.service import (
    create_call_outcome,
    create_follow_up,
    set_call_outcome,
    update_call_outcome,
)
from voiceagent.tenancy import TenantContext


@pytest.fixture
def context() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), membership_id=uuid.uuid4())


def test_create_call_outcome_rejects_invalid_outcome_value(context: TenantContext) -> None:
    with pytest.raises(InvalidOutcomeValueError):
        create_call_outcome(context, uuid.uuid4(), outcome="not-a-real-outcome")


def test_set_call_outcome_rejects_invalid_outcome_value(context: TenantContext) -> None:
    with pytest.raises(InvalidOutcomeValueError):
        set_call_outcome(context, uuid.uuid4(), outcome="not-a-real-outcome")


def test_update_call_outcome_rejects_invalid_outcome_value(context: TenantContext) -> None:
    with pytest.raises(InvalidOutcomeValueError):
        update_call_outcome(context, uuid.uuid4(), outcome="not-a-real-outcome")


def test_create_follow_up_rejects_invalid_type(context: TenantContext) -> None:
    with pytest.raises(InvalidFollowUpTypeError):
        create_follow_up(context, uuid.uuid4(), type="not-a-real-type")


def test_create_follow_up_rejects_calendar_event_id_for_non_appointment_type(
    context: TenantContext,
) -> None:
    """Brief §8: `calendar_event_id` is only valid for `type == 'appointment'`
    -- rejected outright, never a silent pending state."""
    with pytest.raises(FollowUpInvalidRelationshipError):
        create_follow_up(
            context,
            uuid.uuid4(),
            type="manual_follow_up",
            calendar_event_id=uuid.uuid4(),
        )
