"""`voiceagent.providers.call_intelligence.registry` (Phase 2.12)."""

from __future__ import annotations

import pytest

from voiceagent.providers.call_intelligence.fakes import FakeCallIntelligenceProvider
from voiceagent.providers.call_intelligence.registry import (
    CALL_INTELLIGENCE_PROVIDERS,
    create_call_intelligence_provider,
)
from voiceagent.providers.registry import UnknownProviderError


def test_fake_provider_is_registered() -> None:
    provider = create_call_intelligence_provider("fake", "fake-model", {})
    assert isinstance(provider, FakeCallIntelligenceProvider)


def test_groq_is_registered() -> None:
    assert "groq" in CALL_INTELLIGENCE_PROVIDERS.known_providers()


def test_unknown_provider_fails_closed() -> None:
    with pytest.raises(UnknownProviderError):
        create_call_intelligence_provider("does-not-exist", "model", {})
