"""Product configuration. See `voiceagent.config.settings` for the rules."""

from __future__ import annotations

from voiceagent.config.settings import (
    DEPLOYMENT_STAGES,
    AiProviderSettings,
    ConfigurationError,
    FreeSwitchSettings,
    ObjectStorageSettings,
    RuntimeSettings,
    Settings,
    get_settings,
    settings_from_env,
)
from voiceagent.config.validation import validate_deployment_readiness

__all__ = [
    "DEPLOYMENT_STAGES",
    "AiProviderSettings",
    "ConfigurationError",
    "FreeSwitchSettings",
    "ObjectStorageSettings",
    "RuntimeSettings",
    "Settings",
    "get_settings",
    "settings_from_env",
    "validate_deployment_readiness",
]
