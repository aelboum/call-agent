"""`/v1/ops` (Phase 2.14): the routes exist, are documented, and are never
reachable without authorization -- mirrors `tests/api/test_app.py`'s own
`client`/`_documented_paths()` fixtures exactly. The routes' actual logic
(Redis heartbeat read, DB-based stuck-call scan) has no hermetic seam here
(both are genuine I/O), so it is exercised through
`voiceagent.runtime.stuck_calls`'s own pure-function tests
(`tests/runtime/test_stuck_calls.py`) instead -- this file only proves the
route is mounted and protected.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from voiceagent.api import build_app


def _documented_paths(app) -> set[str]:
    return set(app.openapi()["paths"])


def test_ops_routes_are_documented(settings) -> None:
    app = build_app(settings)
    paths = _documented_paths(app)
    assert "/v1/ops/runtime-heartbeats" in paths
    assert "/v1/ops/stuck-calls" in paths


def test_ops_routes_are_never_reachable_without_authorization(settings) -> None:
    """No credential presented -- neither route may return 200 or 500; both
    must fail the same authorization gate every other tenant-scoped route in
    this API already goes through."""
    client = TestClient(build_app(settings))
    for path in ("/v1/ops/runtime-heartbeats", "/v1/ops/stuck-calls"):
        response = client.get(path)
        assert response.status_code in (401, 403), f"{path} returned {response.status_code}"
