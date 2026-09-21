"""The AI call-agent platform's product package.

Layering (Phase 0 report section 4.1, binding):

    voiceagent -> core.* / infra.* / api.* / control_plane.*   ALLOWED
    core.* / infra.*  -> voiceagent                            FORBIDDEN, always

This module is deliberately import-cheap: it defines a version string and
nothing else. Importing `voiceagent` must never open a database connection,
read a secret, construct a provider client, or touch the network -- several
tests assert exactly that.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
