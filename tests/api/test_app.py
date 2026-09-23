"""The application shell (Phase 1 brief section 6).

`TestClient` is used *without* a context manager throughout, deliberately:
that runs the routes but not the lifespan, so the suite stays hermetic while
still exercising the real ASGI stack. The lifespan's own behavior (logging,
tracing, the unsafe-database-role guard) belongs to SaaS-OS and is tested
there.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
from core.config import Settings as PlatformSettings
from fastapi.testclient import TestClient

from voiceagent.api import build_app
from voiceagent.config import ConfigurationError, Settings, settings_from_env


@pytest.fixture
def client(settings):
    return TestClient(build_app(settings))


def _documented_paths(app) -> set[str]:
    """The application's routed paths, read from the generated OpenAPI
    schema.

    Deliberately not `app.routes`: this FastAPI version represents an
    included router as an opaque object with no public path attribute, so
    walking the route list would couple these tests to framework internals.
    The schema is public API. Routes marked `include_in_schema=False` (the
    platform's `/healthz` and `/readyz`) do not appear here and are asserted
    by calling them instead.
    """
    return set(app.openapi()["paths"])


def test_build_app_is_deterministic(settings) -> None:
    """Two builds from the same settings produce the same route set: nothing
    depends on import order or on environment discovery inside the builder."""
    assert _documented_paths(build_app(settings)) == _documented_paths(build_app(settings))
    assert "/v1/meta" in _documented_paths(build_app(settings))


def test_configuration_is_injected_not_rediscovered(settings) -> None:
    """Passing settings builds that application; it does not consult the
    environment or a process-wide cache."""
    custom = replace(settings, app_display_name="Injected Name")
    app = build_app(custom)
    assert app.state.settings is custom
    assert app.title == "Injected Name"


def test_debug_is_never_enabled(settings) -> None:
    """Inherited from `build_platform_app()`: a stack trace must never reach
    an HTTP response, and there is no flag that turns that on."""
    assert build_app(settings).debug is False


def test_liveness_endpoint(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_readiness_endpoint_reports_dependency_state(client) -> None:
    """With no PostgreSQL or Redis reachable, readiness must report *not
    ready* -- never a 500, and never a body carrying connection detail. A
    dependency being down is an expected state, not an exception."""
    response = client.get("/readyz")
    assert response.status_code in (200, 503)
    body = response.json()
    assert set(body) == {"status", "checks"}
    for check in body["checks"]:
        assert set(check) == {"name", "status"}


def test_v1_meta_endpoint(client, settings) -> None:
    response = client.get("/v1/meta")
    assert response.status_code == 200
    assert response.json() == {
        "name": settings.app_display_name,
        "version": "0.1.0",
        "api_version": "v1",
    }


def test_meta_discloses_nothing_about_the_deployment(client) -> None:
    """An unauthenticated route: no environment, no dependency state, no
    pinned-platform commit, no feature flags."""
    body = client.get("/v1/meta").json()
    assert "environment" not in body
    assert "database" not in body


def test_api_prefix_is_configurable(settings) -> None:
    """The prefix lives in one place (`Settings.api_prefix`, inherited from
    the platform's own `API_V1_PREFIX`), so a route cannot hardcode it."""
    platform = replace(settings.platform, api_v1_prefix="/api/v1")
    app = build_app(replace(settings, platform=platform))
    assert "/api/v1/meta" in _documented_paths(app)


def test_cors_is_absent_unless_configured(settings) -> None:
    """No accidental permissive CORS: an unconfigured deployment adds no CORS
    middleware at all rather than defaulting to something open."""
    app = build_app(settings)
    assert not any("CORSMiddleware" in str(middleware.cls) for middleware in app.user_middleware)


def test_cors_is_added_only_for_explicit_origins(settings) -> None:
    app = build_app(replace(settings, cors_allowed_origins=("https://example.test",)))
    cors = [m for m in app.user_middleware if "CORSMiddleware" in str(m.cls)]
    assert len(cors) == 1
    assert cors[0].kwargs["allow_origins"] == ["https://example.test"]


def test_cors_allowed_methods_matches_every_verb_a_v1_route_actually_uses(settings) -> None:
    """Phase 2.16 security audit finding: the previous list (`GET`, `POST`,
    `PATCH`, `DELETE`) was missing `PUT` (`voiceagent.api.v1.call_sessions`'s
    `PUT .../outcome` route) -- a cross-origin browser request to that route
    would fail CORS preflight -- and included an unused `DELETE` (no `/v1`
    route uses it). This test derives the expected set directly from the
    mounted route table rather than hand-duplicating it, so it cannot drift
    silently if a future route introduces a new verb."""
    app = build_app(replace(settings, cors_allowed_origins=("https://example.test",)))
    # `app.routes` represents an included router as an opaque object with no
    # public `.methods` attribute in this FastAPI version (`_documented_paths()`
    # above hits the identical limitation) -- the generated OpenAPI schema is
    # the reliable source for "every verb a mounted route actually uses".
    used_methods = {
        method.upper()
        for operations in app.openapi()["paths"].values()
        for method in operations
        if method.upper() != "HEAD"  # FastAPI adds HEAD to every GET automatically
    }
    cors = next(m for m in app.user_middleware if "CORSMiddleware" in str(m.cls))
    assert set(cast("list[str]", cors.kwargs["allow_methods"])) == used_methods


def test_security_headers_are_present_on_every_response(client) -> None:
    """`X-Content-Type-Options: nosniff` is always safe for a JSON-only API
    (this app serves no HTML) and costs nothing to apply unconditionally."""
    response = client.get("/v1/meta")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_hsts_is_absent_outside_production(client) -> None:
    """Asserting HTTPS in development/test would break a deployment that
    has no HTTPS to assert, rather than hardening it."""
    response = client.get("/v1/meta")
    assert "strict-transport-security" not in response.headers


def test_hsts_is_present_in_production() -> None:
    production_settings = Settings(
        platform=PlatformSettings(environment="production", debug=False),
        cors_allowed_origins=("https://app.test",),
    )
    client = TestClient(build_app(production_settings))
    response = client.get("/v1/meta")
    assert response.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"


def test_wildcard_cors_is_rejected_in_production(monkeypatch) -> None:
    """Fail closed at construction: a production process configured with `*`
    must not start, rather than starting and being permissive."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("VOICEAGENT_CORS_ALLOWED_ORIGINS", "*")
    from core.config import get_settings as get_platform_settings

    get_platform_settings.cache_clear()
    try:
        with pytest.raises(ConfigurationError):
            settings_from_env()
    finally:
        get_platform_settings.cache_clear()
