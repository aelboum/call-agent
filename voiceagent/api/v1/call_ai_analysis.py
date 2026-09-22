"""`/v1/call-sessions/{call_session_id}/ai-analysis` -- the minimal surface
Phase 2.12 needs: read the latest AI post-call analysis, and request one
(idempotent create, or an explicit authorized rebuild).

Deliberately not a general provider-execution endpoint (brief API: "Do not
allow arbitrary users to invoke provider execution directly"): this route
only ever creates or reads a `CallAiAnalysis` *row*.
`voiceagent.call_intelligence.worker.CallAiAnalysisWorker` is the only thing
that ever actually invokes the AI provider, on its own bounded schedule --
a rebuild request here queues a fresh version for that worker to pick up;
it does not run inline, never blocks, and returns immediately with the
still-`pending` row.

Returns only validated application-level structures
(`voiceagent.call_intelligence.schema.CallAiAnalysisResult`, via
`CallAiAnalysisOut`) -- never a raw provider response, never a prompt.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from voiceagent.api.errors import conflict, not_found
from voiceagent.call_intelligence.errors import (
    CallAiAnalysisInProgressError,
    CallAiAnalysisNotFoundError,
    CallNotEligibleForAnalysisError,
)
from voiceagent.call_intelligence.models import CallAiAnalysis
from voiceagent.call_intelligence.permissions import RESOURCE
from voiceagent.call_intelligence.prompt import PROMPT_VERSION
from voiceagent.call_intelligence.schema import CallAiAnalysisResult
from voiceagent.call_intelligence.service import get_latest_analysis, request_analysis
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.config.settings import get_settings
from voiceagent.tenancy import TenantContext, require_tenant

router = APIRouter(prefix="/call-sessions/{call_session_id}/ai-analysis", tags=["call-ai-analysis"])

_read = require_tenant(RESOURCE, "read")
_rebuild = require_tenant(RESOURCE, "rebuild")


class CallAiAnalysisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    call_session_id: uuid.UUID
    version: int
    status: str
    schema_version: str
    prompt_version: str
    provider: str
    model: str
    attempt_count: int
    requested_at: datetime
    completed_at: datetime | None
    failure_reason: str | None
    result: CallAiAnalysisResult | None

    @classmethod
    def from_model(cls, row: CallAiAnalysis) -> CallAiAnalysisOut:
        return cls(
            id=row.id,
            call_session_id=row.call_session_id,
            version=row.version,
            status=row.status,
            schema_version=row.schema_version,
            prompt_version=row.prompt_version,
            provider=row.provider,
            model=row.model,
            attempt_count=row.attempt_count,
            requested_at=row.requested_at,
            completed_at=row.completed_at,
            failure_reason=row.failure_reason,
            result=(
                CallAiAnalysisResult.model_validate(row.result) if row.result is not None else None
            ),
        )


@router.get("")
def get_call_ai_analysis_route(
    call_session_id: uuid.UUID,
    context: TenantContext = Depends(_read),  # noqa: B008
) -> CallAiAnalysisOut:
    try:
        row = get_latest_analysis(context, call_session_id)
    except CallAiAnalysisNotFoundError:
        raise not_found("call AI analysis") from None
    return CallAiAnalysisOut.from_model(row)


@router.post("/rebuild", status_code=201)
def rebuild_call_ai_analysis_route(
    call_session_id: uuid.UUID,
    context: TenantContext = Depends(_rebuild),  # noqa: B008
) -> CallAiAnalysisOut:
    settings = get_settings().call_intelligence
    try:
        row = request_analysis(
            context,
            call_session_id,
            provider=settings.provider,
            model=settings.model,
            prompt_version=PROMPT_VERSION,
            force_rebuild=True,
        )
    except CallSessionNotFoundError:
        raise not_found("call session") from None
    except CallNotEligibleForAnalysisError:
        raise conflict("Call has not completed yet.") from None
    except CallAiAnalysisInProgressError:
        raise conflict("An AI analysis is already in progress for this call.") from None
    return CallAiAnalysisOut.from_model(row)
