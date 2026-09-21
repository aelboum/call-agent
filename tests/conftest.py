"""Test configuration.

The suite is hermetic by design: no PostgreSQL, no Redis, no network, no
secret store, no AI provider, no FreeSWITCH. Everything Phase 1 builds can be
constructed and asserted without them, and a test that needs a real database
would be a Phase 2 integration test with its own marker.

`DATABASE_URL` and `REDIS_URL` are set to syntactically valid values that are
never connected to. Both must be *set*: importing `api.dependencies` reaches
`core.identity.session_retention`, which registers an ARQ job at module scope
and therefore reads the jobs configuration during import (a Phase 1 finding
about the platform's import surface, recorded in `docs/PHASE-1-STATUS.md`).
Reading configuration is not connecting: nothing in this suite opens a socket.
The FastAPI lifespan, which would, is entered only by a `TestClient` used as a
context manager -- which no test here does.

Both URLs point at port 1 deliberately. If any import or any `build_app()`
call ever did open a connection, it would fail immediately and loudly rather
than quietly succeeding against a developer's local database.
"""

from __future__ import annotations

import os

import pytest

_TEST_ENVIRONMENT = {
    "ENVIRONMENT": "test",
    "DEBUG": "false",
    "LOG_LEVEL": "WARNING",
    "DATABASE_URL": "postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
    "REDIS_URL": "redis://127.0.0.1:1/0",
}


def pytest_configure() -> None:
    for name, value in _TEST_ENVIRONMENT.items():
        os.environ.setdefault(name, value)


@pytest.fixture
def settings():
    """Product settings built from the test environment, not from the cache."""
    from voiceagent.config import settings_from_env

    return settings_from_env()
