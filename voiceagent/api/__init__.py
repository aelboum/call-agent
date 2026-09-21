"""The product's HTTP application.

`build_app()` is the composition root; `voiceagent.api.asgi` holds the
module-level instance a process server binds to.
"""

from __future__ import annotations

from voiceagent.api.app import build_app

__all__ = ["build_app"]
