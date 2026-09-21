"""A typed, mutable provider registry (Phase 2.3 brief: "provider selection
must happen through typed configuration/factory resolution, not through
hardcoded provider logic").

One instance of `ProviderRegistry[T]` exists per pipeline component type
(`voiceagent.providers.stt.registry.STT_PROVIDERS`,
`voiceagent.providers.llm.registry.LLM_PROVIDERS`,
`voiceagent.providers.tts.registry.TTS_PROVIDERS`, and the realtime-provider
registry in `voiceagent.providers.engines.factory`). Every one of them is
built the same way: a name maps to a factory function that turns an
`AgentVersion`'s per-component `config` mapping into a live provider
instance. `voiceagent.providers.engines.factory.build_conversation_engine()`
is the only caller that matters -- it depends on these registries, never on
`if provider == "...":` branching, which is what makes adding a provider a
one-call `register()` rather than an engine/runtime change (brief: "Adding a
new provider should require only: 1. provider adapter, 2. provider-specific
configuration, 3. provider adapter tests, 4. registration in the provider
resolver").

This module knows nothing about STT, LLM, TTS or any vendor -- it is the
mechanism, not a policy.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

__all__ = ["ProviderRegistry", "UnknownProviderError"]

type ProviderFactory[T] = Callable[[Mapping[str, object]], T]


class UnknownProviderError(Exception):
    """Raised when an `AgentVersion` names a provider no registry knows.

    A configuration error, not a code error: the fix is either a typo in the
    agent's published configuration or a missing `register()` call for a
    provider that genuinely does not exist yet -- never a reason to add a
    branch to the engine or the runtime.
    """

    def __init__(self, component: str, name: str, known: tuple[str, ...]) -> None:
        registered = ", ".join(known) or "(none)"
        message = f"unknown {component} provider {name!r}; registered providers: {registered}"
        super().__init__(message)
        self.component = component
        self.name = name
        self.known = known


class ProviderRegistry[T]:
    """Name -> factory, for one pipeline component type.

    `create()` is the only thing `voiceagent.providers.engines.factory`
    calls; `register()` is the only thing a new provider adapter's own
    module needs to call to become selectable from `AgentVersion.config`.
    Neither method has any vendor knowledge -- that lives entirely inside
    the factory functions registered here, each confined to its own adapter
    module (enforced by `tests/architecture/test_import_boundaries.py` and
    the matching import-linter contracts in `pyproject.toml`).
    """

    def __init__(self, component: str) -> None:
        self._component = component
        self._factories: dict[str, ProviderFactory[T]] = {}

    def register(self, name: str, factory: ProviderFactory[T]) -> None:
        """Idempotent by name: registering the same name twice replaces the
        factory rather than raising, so a test can substitute a double
        without importing internals."""
        self._factories[name] = factory

    def create(self, name: str, config: Mapping[str, object]) -> T:
        try:
            factory = self._factories[name]
        except KeyError:
            raise UnknownProviderError(self._component, name, self.known_providers()) from None
        return factory(config)

    def known_providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))
