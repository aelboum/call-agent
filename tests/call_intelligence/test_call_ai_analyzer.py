"""`voiceagent.call_intelligence.analyzer.run_call_ai_analysis()` (Phase
2.12).

Every database-touching collaborator (`service.claim_pending_analysis`/
`complete_analysis`/`fail_analysis`, `prompt.build_analysis_input`, and the
module's own `_authorize()`) is monkeypatched at the point `analyzer.py`
actually calls it through -- exactly the technique
`tests/tools/test_gateway.py`'s own module docstring describes for
hermetically testing a function that, in production, always crosses a real
database. Only `voiceagent.providers.call_intelligence.fakes
.FakeCallIntelligenceProvider` is real, standing in for the network call.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from voiceagent.call_intelligence import analyzer as analyzer_module
from voiceagent.call_intelligence import prompt as prompt_module
from voiceagent.call_intelligence import service as service_module
from voiceagent.call_intelligence.analyzer import run_call_ai_analysis
from voiceagent.call_intelligence.prompt import ANALYZER_SYSTEM_INSTRUCTIONS, AnalysisInput
from voiceagent.config.settings import AiProviderSettings
from voiceagent.providers.call_intelligence.contracts import (
    CallIntelligenceErrorCode,
    CallIntelligenceProviderError,
)
from voiceagent.providers.call_intelligence.fakes import FakeCallIntelligenceProvider
from voiceagent.runtime.db import DatabaseBoundary
from voiceagent.runtime.errors import DataAuthorizationDeniedError
from voiceagent.runtime.privacy import PrivacyConfigurationError
from voiceagent.tenancy import TenantContext


@pytest.fixture
def db():
    boundary = DatabaseBoundary(max_workers=4)
    yield boundary
    boundary.close()


def _context() -> TenantContext:
    return TenantContext(tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), membership_id=uuid.uuid4())


def _claimed_row() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), execution_id=uuid.uuid4(), call_session_id=uuid.uuid4())


def _analysis_input() -> AnalysisInput:
    return AnalysisInput(
        system_instructions=ANALYZER_SYSTEM_INSTRUCTIONS,
        call_metadata={"duration_ms": 1000},
        agent_instructions="Be helpful.",
        transcript_text="user: hi\nassistant: hello",
    )


def _run(coro):
    return asyncio.run(coro)


def _patch_happy_claim(monkeypatch: pytest.MonkeyPatch, claimed) -> None:
    monkeypatch.setattr(service_module, "claim_pending_analysis", lambda ctx, now=None: claimed)
    monkeypatch.setattr(analyzer_module, "_authorize", lambda **kwargs: None)
    monkeypatch.setattr(
        prompt_module, "build_analysis_input", lambda ctx, call_session_id: _analysis_input()
    )


def _kwargs(db, provider, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        db=db,
        context=_context(),
        provider=provider,
        provider_name="fake",
        ai_provider_settings=AiProviderSettings(),
        system_actor_user_id=uuid.uuid4(),
        timeout_seconds=1.0,
    )
    base.update(overrides)
    return base


def test_nothing_to_claim_returns_none(db, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "claim_pending_analysis", lambda ctx, now=None: None)
    result = _run(run_call_ai_analysis(**_kwargs(db, FakeCallIntelligenceProvider())))
    assert result is None


def test_successful_analysis_completes(db, monkeypatch: pytest.MonkeyPatch) -> None:
    claimed = _claimed_row()
    _patch_happy_claim(monkeypatch, claimed)
    completed = SimpleNamespace(id=claimed.id, status="completed")
    captured = {}

    def _fake_complete(ctx, analysis_id, *, execution_id, result):
        captured["analysis_id"] = analysis_id
        captured["execution_id"] = execution_id
        captured["result"] = result
        return completed

    monkeypatch.setattr(service_module, "complete_analysis", _fake_complete)

    provider = FakeCallIntelligenceProvider()
    result = _run(run_call_ai_analysis(**_kwargs(db, provider)))

    assert result is completed
    assert captured["analysis_id"] == claimed.id
    assert captured["execution_id"] == claimed.execution_id
    assert captured["result"].confidence == 0.5
    assert len(provider.requests) == 1
    assert provider.requests[0].system_instructions == ANALYZER_SYSTEM_INSTRUCTIONS


def test_privacy_denied_fails_without_invoking_the_provider(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = _claimed_row()
    monkeypatch.setattr(service_module, "claim_pending_analysis", lambda ctx, now=None: claimed)

    def _deny(**kwargs):
        raise DataAuthorizationDeniedError(claimed.call_session_id, "policy_denied")

    monkeypatch.setattr(analyzer_module, "_authorize", _deny)

    captured = {}

    def _fake_fail(ctx, analysis_id, *, execution_id, reason):
        captured["reason"] = reason
        return SimpleNamespace(id=analysis_id, status="failed", failure_reason=reason)

    monkeypatch.setattr(service_module, "fail_analysis", _fake_fail)

    provider = FakeCallIntelligenceProvider()
    result = _run(run_call_ai_analysis(**_kwargs(db, provider)))

    assert captured["reason"] == "privacy_denied"
    assert result is not None
    assert result.status == "failed"
    assert len(provider.requests) == 0  # the provider must never be invoked


def test_privacy_misconfigured_fails_as_unexpected_error(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed = _claimed_row()
    monkeypatch.setattr(service_module, "claim_pending_analysis", lambda ctx, now=None: claimed)

    def _misconfigured(**kwargs):
        raise PrivacyConfigurationError("not configured")

    monkeypatch.setattr(analyzer_module, "_authorize", _misconfigured)
    captured = {}
    monkeypatch.setattr(
        service_module,
        "fail_analysis",
        lambda ctx, analysis_id, *, execution_id, reason: captured.setdefault("reason", reason),
    )

    provider = FakeCallIntelligenceProvider()
    _run(run_call_ai_analysis(**_kwargs(db, provider)))

    assert captured["reason"] == "unexpected_error"
    assert len(provider.requests) == 0


def test_provider_timeout_is_recorded(db, monkeypatch: pytest.MonkeyPatch) -> None:
    claimed = _claimed_row()
    _patch_happy_claim(monkeypatch, claimed)
    captured = {}
    monkeypatch.setattr(
        service_module,
        "fail_analysis",
        lambda ctx, analysis_id, *, execution_id, reason: captured.setdefault("reason", reason),
    )

    provider = FakeCallIntelligenceProvider(delay_seconds=1.0)
    _run(run_call_ai_analysis(**_kwargs(db, provider, timeout_seconds=0.05)))

    assert captured["reason"] == "provider_timeout"


@pytest.mark.parametrize(
    "code, expected_reason",
    [
        (CallIntelligenceErrorCode.AUTH, "provider_error"),
        (CallIntelligenceErrorCode.RATE_LIMIT, "provider_error"),
        (CallIntelligenceErrorCode.INVALID_REQUEST, "provider_error"),
        (CallIntelligenceErrorCode.TRANSIENT, "provider_error"),
        (CallIntelligenceErrorCode.PROVIDER_DOWN, "provider_unavailable"),
        (CallIntelligenceErrorCode.TIMEOUT, "provider_timeout"),
        (CallIntelligenceErrorCode.MALFORMED_RESPONSE, "malformed_response"),
    ],
)
def test_provider_error_codes_are_normalized(
    db, monkeypatch: pytest.MonkeyPatch, code, expected_reason
) -> None:
    claimed = _claimed_row()
    _patch_happy_claim(monkeypatch, claimed)
    captured = {}
    monkeypatch.setattr(
        service_module,
        "fail_analysis",
        lambda ctx, analysis_id, *, execution_id, reason: captured.setdefault("reason", reason),
    )

    provider = FakeCallIntelligenceProvider(error=CallIntelligenceProviderError(code, "boom"))
    _run(run_call_ai_analysis(**_kwargs(db, provider)))

    assert captured["reason"] == expected_reason


def test_malformed_provider_response_is_rejected(db, monkeypatch: pytest.MonkeyPatch) -> None:
    claimed = _claimed_row()
    _patch_happy_claim(monkeypatch, claimed)
    captured = {}
    monkeypatch.setattr(
        service_module,
        "fail_analysis",
        lambda ctx, analysis_id, *, execution_id, reason: captured.setdefault("reason", reason),
    )

    provider = FakeCallIntelligenceProvider(response_text="not valid json at all")
    _run(run_call_ai_analysis(**_kwargs(db, provider)))

    assert captured["reason"] == "malformed_response"


def test_schema_violating_provider_response_is_rejected(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid JSON, but missing required fields -- also a `malformed_response`."""
    claimed = _claimed_row()
    _patch_happy_claim(monkeypatch, claimed)
    captured = {}
    monkeypatch.setattr(
        service_module,
        "fail_analysis",
        lambda ctx, analysis_id, *, execution_id, reason: captured.setdefault("reason", reason),
    )

    provider = FakeCallIntelligenceProvider(response_text='{"summary": "only this field"}')
    _run(run_call_ai_analysis(**_kwargs(db, provider)))

    assert captured["reason"] == "malformed_response"


def test_cancellation_propagates_and_records_no_outcome(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled analysis mid-provider-call must not be recorded as
    completed or failed -- the claim lease naturally expires later, letting
    `claim_pending_analysis()` reclaim it (stale-processing recovery), the
    same recovery path a crashed worker relies on."""
    claimed = _claimed_row()
    _patch_happy_claim(monkeypatch, claimed)
    complete_calls = []
    fail_calls = []
    monkeypatch.setattr(
        service_module, "complete_analysis", lambda *a, **k: complete_calls.append(1)
    )
    monkeypatch.setattr(service_module, "fail_analysis", lambda *a, **k: fail_calls.append(1))

    provider = FakeCallIntelligenceProvider(delay_seconds=10.0)

    async def _scenario():
        task = asyncio.create_task(
            run_call_ai_analysis(**_kwargs(db, provider, timeout_seconds=5.0))
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(_scenario())

    assert complete_calls == []
    assert fail_calls == []
