"""The product's application composition root.

`build_app()` is the product's own entrypoint builder (SaaS-OS ADR-0017: a
consuming project owns its application composition and must never import a
complete platform application as if it were its own). It calls the platform's
reusable builder and mounts the product's routes on top -- it never imports
`api.main:app` or `api.server`.

What `api.platform.build_platform_app()` already provides, and this module
therefore does not rebuild: `FastAPI(debug=False)` with no flag to turn that
on; a lifespan that configures structured logging and tracing and then runs
the fail-closed guard rejecting an unsafe (superuser/`BYPASSRLS`) database
role; correlation-ID middleware ahead of every route; the liveness/readiness
routes (`/healthz`, `/readyz`); and the OIDC login routes.

Construction invariants asserted by `tests/api/`:

* **Deterministic.** Two calls with the same settings produce the same route
  set. No ordering depends on import order or environment discovery.
* **Configuration is injected**, never re-read from the environment inside the
  builder: `build_app(settings=...)` is how tests and future composition
  variants work, and the resolved settings live on `app.state`, which is
  per-application, not a process global.
* **No I/O at import or build time.** No database connection, no Redis, no
  secret read, no provider client, no FreeSWITCH, no Pipecat. Startup work
  belongs to the lifespan, which only runs when the app actually runs.
* **No global mutable tenant state.** There is none to have: tenant context is
  a value derived per request (`voiceagent.tenancy`).
"""

from __future__ import annotations

from api.platform import build_platform_app
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from voiceagent import __version__
from voiceagent.api.v1 import router as v1_router
from voiceagent.config import Settings, get_settings

__all__ = ["build_app"]


def build_app(settings: Settings | None = None) -> FastAPI:
    """Build the product's FastAPI application.

    `settings` defaults to the process-wide configuration; pass an explicit
    `Settings` to build a differently-configured application without touching
    the environment or a cache.
    """
    resolved = settings if settings is not None else get_settings()

    app = build_platform_app(title=resolved.app_display_name, version=__version__)
    app.state.settings = resolved

    # CORS is off unless origins are configured explicitly. There is no
    # wildcard path: `Settings` rejects "*" in production, and an empty
    # configuration adds no middleware at all rather than defaulting to
    # something permissive.
    if resolved.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        )

    app.include_router(v1_router, prefix=resolved.api_prefix)
    return app
