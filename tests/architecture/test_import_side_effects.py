"""Importing and building the product must open no connection.

"No database access on import" and "no provider initialization on import" are
the kind of property that is true until someone adds one convenient
module-level call, and then silently false everywhere -- in tests, in tooling,
in a migration runner, in a `--help` invocation.

Each check runs in a *subprocess pointed at unreachable endpoints*:
`DATABASE_URL` and `REDIS_URL` are syntactically valid but resolve to port 1,
so anything that actually tried to connect would fail immediately instead of
quietly succeeding against a developer's local services. The subprocess
exiting 0 is therefore evidence that no connection was attempted.

Both variables must be *set*, because importing `api.dependencies` reaches
`core.identity.session_retention`, which registers an ARQ job at module scope
and reads the jobs configuration during import. That is a configuration read,
not a connection -- the distinction these tests exist to hold.
"""

from __future__ import annotations

import os
import subprocess
import sys

_UNREACHABLE = {
    "ENVIRONMENT": "test",
    "DATABASE_URL": "postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
    "REDIS_URL": "redis://127.0.0.1:1/0",
}


def _run_in_subprocess(statement: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **_UNREACHABLE}
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, no user input
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


def test_importing_the_package_needs_no_configuration_at_all() -> None:
    """`voiceagent` itself defines a version string and nothing else: no
    settings read, no platform import, no I/O."""
    env = {key: value for key, value in os.environ.items() if key not in _UNREACHABLE}
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, no user input
        [sys.executable, "-c", "import voiceagent; print(voiceagent.__version__)"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.1.0"


def test_importing_the_composition_root_opens_no_connection() -> None:
    """Importing `build_app` must not construct an application, let alone
    connect to anything -- which is why the module-level instance lives in
    `voiceagent.api.asgi` instead."""
    result = _run_in_subprocess("from voiceagent.api import build_app; print(callable(build_app))")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_importing_contracts_initializes_no_provider() -> None:
    """The telephony, engine and storage contracts are pure declarations: no
    client, no SDK, no connection, no credential lookup."""
    result = _run_in_subprocess(
        "import voiceagent.telephony, voiceagent.providers.engines, "
        "voiceagent.providers.objectstore; print('ok')"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_building_the_app_opens_no_connection() -> None:
    """`build_app()` itself does no I/O. The database is touched by the
    platform lifespan -- which runs only when the application actually runs,
    and whose unsafe-role guard would fail closed against port 1."""
    result = _run_in_subprocess(
        "from voiceagent.api import build_app; "
        "from voiceagent.config import settings_from_env; "
        "app = build_app(settings_from_env()); print(len(app.routes) > 0)"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
