"""Product configuration. See `voiceagent.config.settings` for the rules."""

from __future__ import annotations

from voiceagent.config.settings import (
    AiProviderSettings,
    ConfigurationError,
    FreeSwitchSettings,
    ObjectStorageSettings,
    Settings,
    get_settings,
    settings_from_env,
)

__all__ = [
    "AiProviderSettings",
    "ConfigurationError",
    "FreeSwitchSettings",
    "ObjectStorageSettings",
    "Settings",
    "get_settings",
    "settings_from_env",
]
