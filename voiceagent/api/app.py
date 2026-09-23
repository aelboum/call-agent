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

from collections.abc import Awaitable, Callable

from api.platform import build_platform_app
from fastapi import FastAPI, Request, Response
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
            # Phase 2.16 security audit: matches the verbs `voiceagent/api
            # /v1/*.py` actually declares -- confirmed by grepping every
            # `@router.<verb>` in that package (26 GET, 20 POST, 3 PATCH, 1
            # PUT, 0 DELETE). The previous list included an unused `DELETE`
            # and was missing `PUT` (`voiceagent.api.v1.call_sessions`'s
            # `PUT .../outcome` route) -- a cross-origin browser request to
            # that one route would have failed CORS preflight even though
            # the route itself works. Keep this list in sync if a future
            # route introduces a verb not yet used anywhere.
            allow_methods=["GET", "POST", "PATCH", "PUT"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        )

    # Phase 2.16 security audit (brief §23/§25): a JSON API response should
    # never be interpreted by a browser as anything other than what its
    # `Content-Type` declares -- `X-Content-Type-Options: nosniff` is a
    # zero-risk, always-safe header for a pure JSON API (this app serves no
    # HTML/static assets at all -- confirmed no `StaticFiles` mount anywhere
    # in this module or `api.platform`, so it cannot break a page render
    # that does not exist). `Strict-Transport-Security` is added only in
    # production, matching every other environment-conditional security
    # control this product already has (`Settings._validate_production()`):
    # asserting HSTS in development would force HTTPS a local/dev deployment
    # may not have, breaking it outright rather than hardening it.
    # `Content-Security-Policy`/`X-Frame-Options` are deliberately not added
    # here -- see `docs/PHASE-2.16-SECURITY-READINESS.md` §25 for why they
    # are a *frontend deployment* concern, not this JSON-only backend's.
    @app.middleware("http")
    async def _security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if resolved.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    app.include_router(v1_router, prefix=resolved.api_prefix)
    return app
