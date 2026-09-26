"""Tier 1 (hermetic, no network/credentials): the generic
`voiceagent.providers.registry.ProviderRegistry` mechanism, and that each
component registry (STT/LLM/TTS) resolves `"fake"` and every real vendor
name with no engine/runtime code involved -- the "provider selection is
configuration/factory-driven, never `if provider == ...`" property, proven
directly against the registries themselves.
"""

from __future__ import annotations

import pytest

from voiceagent.providers.engines.component_fakes import (
    FakeLlmProvider,
    FakeSttProvider,
    FakeTtsProvider,
)
from voiceagent.providers.llm.registry import LLM_PROVIDERS, create_llm_provider
from voiceagent.providers.registry import ProviderRegistry, UnknownProviderError
from voiceagent.providers.stt.registry import STT_PROVIDERS, create_stt_provider
from voiceagent.providers.tts.registry import TTS_PROVIDERS, create_tts_provider


def test_generic_registry_creates_via_a_registered_factory() -> None:
    registry: ProviderRegistry[str] = ProviderRegistry("widget")
    registry.register("upper", lambda config: str(config["text"]).upper())
    assert registry.create("upper", {"text": "hi"}) == "HI"


def test_generic_registry_raises_a_typed_error_for_an_unknown_name() -> None:
    registry: ProviderRegistry[str] = ProviderRegistry("widget")
    registry.register("known", lambda config: "ok")
    with pytest.raises(UnknownProviderError) as exc_info:
        registry.create("unknown", {})
    assert exc_info.value.component == "widget"
    assert exc_info.value.name == "unknown"
    assert exc_info.value.known == ("known",)


def test_generic_registry_registration_is_replace_not_append() -> None:
    registry: ProviderRegistry[str] = ProviderRegistry("widget")
    registry.register("a", lambda config: "first")
    registry.register("a", lambda config: "second")
    assert registry.create("a", {}) == "second"
    assert registry.known_providers() == ("a",)


def test_stt_registry_knows_every_provider_this_phase_ships() -> None:
    assert set(STT_PROVIDERS.known_providers()) == {"assemblyai", "deepgram", "fake"}


def test_llm_registry_knows_every_provider_this_phase_ships() -> None:
    assert set(LLM_PROVIDERS.known_providers()) == {"fake", "gemini", "groq", "mistral", "openai"}


def test_tts_registry_knows_every_provider_this_phase_ships() -> None:
    assert set(TTS_PROVIDERS.known_providers()) == {"deepgram_aura", "elevenlabs", "fake"}


def test_create_stt_provider_fake_returns_a_fresh_fake_each_time() -> None:
    first = create_stt_provider("fake", {})
    second = create_stt_provider("fake", {})
    assert isinstance(first, FakeSttProvider)
    assert first is not second


def test_create_llm_provider_fake_ignores_model_and_config() -> None:
    provider = create_llm_provider("fake", "any-model", {})
    assert isinstance(provider, FakeLlmProvider)


def test_create_tts_provider_fake_returns_a_fake() -> None:
    assert isinstance(create_tts_provider("fake", {}), FakeTtsProvider)


def test_create_stt_provider_raises_for_an_unregistered_name() -> None:
    with pytest.raises(UnknownProviderError):
        create_stt_provider("not-a-real-vendor", {})


def test_create_llm_provider_raises_for_an_unregistered_name() -> None:
    with pytest.raises(UnknownProviderError):
        create_llm_provider("not-a-real-vendor", "model", {})


def test_create_tts_provider_raises_for_an_unregistered_name() -> None:
    with pytest.raises(UnknownProviderError):
        create_tts_provider("not-a-real-vendor", {})


def test_a_second_fake_provider_satisfies_the_registry_with_no_engine_change() -> None:
    """The brief's own provider-independence bar: "a second fake
    implementation should be able to satisfy the protocol without
    modifying the runtime" -- proven here at the registry itself by
    registering a structurally different STT double under a new name."""

    class _AlternateFakeStt:
        async def stream(self, audio):
            async for _ in audio:
                pass
            return
            yield  # pragma: no cover

    STT_PROVIDERS.register("test-alternate", lambda config: _AlternateFakeStt())
    try:
        provider = create_stt_provider("test-alternate", {})
        assert isinstance(provider, _AlternateFakeStt)
    finally:
        # Registries are process-wide singletons -- deregister by
        # restoring the registry's own known set so this test cannot leak
        # a name into any test that runs after it.
        STT_PROVIDERS._factories.pop("test-alternate", None)  # noqa: SLF001 -- test-only cleanup.
