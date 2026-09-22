"""`run_call_ai_analysis()` -- the one place a `CallAiAnalysis` claim, a
privacy check, an external provider call, and a persisted outcome are
actually wired together (Phase 2.12).

Reached only from `voiceagent.call_intelligence.worker.CallAiAnalysisWorker`
-- itself a bounded polling loop, never the audio pump, never a Tool Gateway
handler (module docstring). Every database-touching step here is one short
`db.run(...)` call; the `await provider.analyze(...)` in between happens
with no transaction open, mirroring `voiceagent.workflows.executor
.run_workflow()`'s own "claim, then external I/O with no lock held, then
persist" shape.

**Privacy (brief PRIVACY).** `_authorize()` below is the one call to
`voiceagent.runtime.privacy.authorize_call_data_access()` in this whole
package -- the exact same `control_plane.data_authorization
.authorize_data_access()` gate the live call runtime already crosses once
per call, invoked again here for a distinct `purpose=POST_CALL_ANALYSIS_PURPOSE`.
No second privacy mechanism is introduced; a tenant/deployment that has not
explicitly allow-listed this purpose (`voiceagent.config.settings
.AiProviderSettings.allowed_purposes`, deployment-wide default) is denied,
fail-closed, exactly like Phase 2.2's own "no per-tenant policy table yet"
limitation already documents. The provider is never invoked when this
check fails or cannot be evaluated.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from pydantic import ValidationError

from voiceagent.call_intelligence import prompt, service
from voiceagent.call_intelligence.models import CallAiAnalysis
from voiceagent.call_intelligence.schema import CallAiAnalysisResult
from voiceagent.calls.errors import CallSessionNotFoundError
from voiceagent.config.settings import AiProviderSettings
from voiceagent.providers.call_intelligence.contracts import (
    CallIntelligenceErrorCode,
    CallIntelligenceProvider,
    CallIntelligenceProviderError,
    CallIntelligenceRequest,
)
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.runtime.errors import DataAuthorizationDeniedError
from voiceagent.runtime.privacy import (
    PrivacyConfigurationError,
    StaticAiDataPolicySource,
    authorize_call_data_access,
)
from voiceagent.tenancy import TenantContext

__all__ = [
    "POST_CALL_ANALYSIS_DATA_CLASSIFICATION",
    "POST_CALL_ANALYSIS_PURPOSE",
    "run_call_ai_analysis",
]

_logger = logging.getLogger(__name__)

#: A distinct purpose from the live call's own `"conversation"` (brief
#: PRIVACY): a tenant/deployment may allow the live engine to see caller
#: audio/transcript without also allowing a third-party post-call analysis
#: provider to see it, and vice versa. Deliberately not configurable --
#: changing what this phase *is* would be a code change, not a deployment
#: knob (matching `voiceagent.runtime.privacy`'s own fixed `"conversation"`
#: precedent, which is likewise a literal in `voiceagent.runtime.call_task`,
#: not a setting).
POST_CALL_ANALYSIS_PURPOSE = "post_call_analysis"
#: Matches `voiceagent.agents.config.PrivacySettings.data_classification`'s
#: own documented default value and `voiceagent.config.settings
#: .AiProviderSettings.allowed_data_classifications`'s own default --
#: post-call analysis reads the same class of data (the call transcript)
#: the live engine already does, so it is authorized under the identical
#: classification, never a new, undocumented one.
POST_CALL_ANALYSIS_DATA_CLASSIFICATION = "tenant_data"

_MAX_OUTPUT_TOKENS = 1024


def _authorize(
    *,
    context: TenantContext,
    call_session_id: uuid.UUID,
    provider_name: str,
    ai_provider_settings: AiProviderSettings,
    system_actor_user_id: uuid.UUID | None,
) -> None:
    """Synchronous -- always called through `DatabaseBoundary.run()` (it
    ultimately writes one `core.audit_log` entry via
    `authorize_data_access()` itself). Raises `DataAuthorizationDeniedError`
    or `PrivacyConfigurationError` exactly as `authorize_call_data_access()`
    does; both are caught by `run_call_ai_analysis()` below and turned into
    a normal `fail_analysis(reason="privacy_denied"/"unexpected_error")`
    outcome, never left to propagate as an unhandled exception."""
    policy_source = StaticAiDataPolicySource(ai_provider_settings)
    authorize_call_data_access(
        tenant_id=context.tenant_id,
        call_session_id=call_session_id,
        data_classification=POST_CALL_ANALYSIS_DATA_CLASSIFICATION,
        purpose=POST_CALL_ANALYSIS_PURPOSE,
        provider=provider_name,
        policy_source=policy_source,
        system_actor_user_id=system_actor_user_id,
    )


async def run_call_ai_analysis(
    *,
    db: DatabaseBoundary,
    context: TenantContext,
    provider: CallIntelligenceProvider,
    provider_name: str,
    ai_provider_settings: AiProviderSettings,
    system_actor_user_id: uuid.UUID | None,
    timeout_seconds: float,
    now: datetime | None = None,
) -> CallAiAnalysis | None:
    """Claim the next due `CallAiAnalysis` for `context`'s tenant and run it
    to completion or failure. Returns `None` when nothing is currently
    eligible -- not an error, the ordinary "nothing to do this tick" result
    `voiceagent.call_intelligence.worker.CallAiAnalysisWorker` polls for."""
    claimed = await db.run(service.claim_pending_analysis, context, now=now)
    if claimed is None:
        return None
    execution_id = claimed.execution_id
    if execution_id is None:  # pragma: no cover -- guaranteed set by claim_pending_analysis()
        raise RuntimeError(f"CallAiAnalysis {claimed.id} claimed with no execution_id")

    try:
        await db.run(
            _authorize,
            context=context,
            call_session_id=claimed.call_session_id,
            provider_name=provider_name,
            ai_provider_settings=ai_provider_settings,
            system_actor_user_id=system_actor_user_id,
        )
    except DataAuthorizationDeniedError:
        return await db.run(
            service.fail_analysis,
            context,
            claimed.id,
            execution_id=execution_id,
            reason="privacy_denied",
        )
    except PrivacyConfigurationError:
        _logger.error("call_ai_analysis_privacy_misconfigured")
        return await db.run(
            service.fail_analysis,
            context,
            claimed.id,
            execution_id=execution_id,
            reason="unexpected_error",
        )

    try:
        analysis_input = await db.run(prompt.build_analysis_input, context, claimed.call_session_id)
    except CallSessionNotFoundError:  # pragma: no cover -- the call row cannot vanish
        # mid-analysis under RLS/foreign-key discipline; handled explicitly
        # rather than left to raise past this function's own boundary.
        return await db.run(
            service.fail_analysis,
            context,
            claimed.id,
            execution_id=execution_id,
            reason="unexpected_error",
        )

    request = CallIntelligenceRequest(
        system_instructions=analysis_input.system_instructions,
        user_content=prompt.render_user_content(analysis_input),
        max_output_tokens=_MAX_OUTPUT_TOKENS,
    )

    try:
        response = await asyncio.wait_for(provider.analyze(request), timeout=timeout_seconds)
    except TimeoutError:
        return await db.run(
            service.fail_analysis,
            context,
            claimed.id,
            execution_id=execution_id,
            reason="provider_timeout",
        )
    except CallIntelligenceProviderError as exc:
        reason = _map_provider_error_reason(exc)
        return await db.run(
            service.fail_analysis, context, claimed.id, execution_id=execution_id, reason=reason
        )

    try:
        result = CallAiAnalysisResult.model_validate_json(response.raw_text)
    except ValidationError:
        return await db.run(
            service.fail_analysis,
            context,
            claimed.id,
            execution_id=execution_id,
            reason="malformed_response",
        )

    return await db.run(
        service.complete_analysis, context, claimed.id, execution_id=execution_id, result=result
    )


def _map_provider_error_reason(exc: CallIntelligenceProviderError) -> str:
    if exc.code is CallIntelligenceErrorCode.PROVIDER_DOWN:
        return "provider_unavailable"
    if exc.code is CallIntelligenceErrorCode.TIMEOUT:
        return "provider_timeout"
    if exc.code is CallIntelligenceErrorCode.MALFORMED_RESPONSE:
        return "malformed_response"
    # AUTH, RATE_LIMIT, INVALID_REQUEST, TRANSIENT -- every other closed
    # provider error code -- is a bounded, backoff-retried "provider_error"
    # (never the raw code or message persisted; see
    # voiceagent.call_intelligence.models.CALL_AI_ANALYSIS_FAILURE_REASONS).
    return "provider_error"
