"""The ASGI entrypoint: `uvicorn voiceagent.api.asgi:app`.

Deliberately separate from `voiceagent.api.app` so that importing the
composition root never constructs an application as a side effect -- a test
imports `build_app`, a server imports this module, and the two cannot
interfere.

Constructing the app here reads configuration from the environment. It still
opens no database connection and initializes no provider: that work belongs to
the platform lifespan, which runs only when the server actually starts.
"""

from __future__ import annotations

from voiceagent.api.app import build_app

app = build_app()
