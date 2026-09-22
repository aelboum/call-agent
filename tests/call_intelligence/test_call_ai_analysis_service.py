"""`voiceagent.call_intelligence.service`'s one pure, pre-`tenant_scope()`
validation path -- exactly like `voiceagent.followups.service
.fail_follow_up_execution()`'s own `reason` check. Every DB-touching path
(request/claim/complete/fail against a real row) is covered by
`tests/integration/test_call_intelligence_integration.py`.
"""

from __future__ import annotations

import uuid

import pytest

from voiceagent.call_intelligence.errors import InvalidCallAiAnalysisFailureReasonError
from voiceagent.call_intelligence.service import fail_analysis
from voiceagent.tenancy import TenantContext


def test_fail_analysis_rejects_an_invalid_reason() -> None:
    context = TenantContext(
        tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), membership_id=uuid.uuid4()
    )
    with pytest.raises(InvalidCallAiAnalysisFailureReasonError):
        fail_analysis(context, uuid.uuid4(), execution_id=uuid.uuid4(), reason="not_a_real_reason")
