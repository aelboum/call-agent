"""Process-level liveness/readiness for the call-runtime and background
worker processes (Phase 2.40; `docs/PHASE-2.36-*` finding: the API has
`/healthz`/`/readyz` -- `api.platform.build_platform_app()`'s own
`infra.health.check_liveness()`/`check_readiness()`, SaaS-OS, off-limits --
but `scripts/run_call_runtime.py`, `scripts/run_call_intelligence_worker.py`
and `scripts/run_followup_worker.py` are plain asyncio processes with no
ASGI server at all, so an orchestrator has no process-level signal for any
of the three).

Spinning up a second FastAPI/uvicorn instance inside each of them merely to
answer two static-shaped probes would duplicate the API server for no
benefit this product needs (brief: "do not duplicate the API server," "do
not introduce a heavyweight web framework merely for two endpoints"). This
module is instead the smallest bounded HTTP listener that can answer
`GET /healthz`/`GET /readyz`, built directly on `asyncio.start_server()` --
no new dependency.

**Liveness is unconditional** (mirrors `infra.health.check_liveness()`
exactly: "if this function can execute at all, the process is live"). It
never depends on PostgreSQL, Redis, FreeSWITCH, or any job/call outcome --
Phase 2.38 made the call-runtime's own heartbeat loop resilient to transient
Redis failures specifically so a Redis blip is never mistaken for a dead
process; this module must not reintroduce that coupling at the health-check
layer.

**Readiness is a one-way-per-lifecycle latch** (`HealthState`), not a live
dependency probe: `mark_ready()` is called once, by each script, only after
that script's own existing startup contract has actually completed (the
exact point varies per process -- see each `scripts/run_*.py`'s own call
site). It is never driven by polling a database or pinging Redis on every
request, which would make an ordinary transient Redis hiccup look identical
to "this process cannot do its job" -- the exact failure mode Phase 2.38
already fixed at the heartbeat layer, and that fix would be undone at this
layer by a naive "ping Redis every probe" readiness check. `mark_not_ready()`
is called once, at the start of each script's own existing shutdown
sequence. A worker job failing, a call failing, or one external AI provider
failing never touches this latch -- only genuine process startup/shutdown
does.

Response bodies reuse `infra.health.HealthStatus`'s own vocabulary
("healthy"/"unhealthy") -- the identical wording `api.health` already
returns -- and carry nothing else: no check detail, no dependency name, no
exception type, no connection string, no tenant/call identifier. The
listener never reads a request body and ignores every header (no auth, no
content negotiation) -- it is meant to be reached only from inside its own
container (a `docker compose` `healthcheck:` entry, matching the API
service's own existing convention), never published as a container port.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from infra.health import HealthStatus

__all__ = ["HealthState", "serve_health_http"]

_logger = logging.getLogger(__name__)

#: Bounds how long this listener waits for a request line/headers, and the
#: maximum size `asyncio.start_server()`'s own `StreamReader` buffers before
#: raising -- a probe that stalls or sends an oversized request must never
#: hang or exhaust memory on a listener nothing else in this process depends
#: on (brief: "cheap and must not perform expensive dependency checks").
_READ_TIMEOUT_SECONDS = 2.0
_MAX_REQUEST_BYTES = 8192


class HealthState:
    """One process's own readiness latch. `is_ready` starts `False` -- a
    process is never ready before its own script calls `mark_ready()` --
    and `mark_not_ready()` is idempotent (safe to call more than once,
    though in practice each script calls it exactly once, at the start of
    its own existing shutdown sequence)."""

    def __init__(self) -> None:
        self._ready = False

    @property
    def is_ready(self) -> bool:
        return self._ready

    def mark_ready(self) -> None:
        self._ready = True

    def mark_not_ready(self) -> None:
        self._ready = False


def _response(status_code: int, status: HealthStatus) -> bytes:
    body = f'{{"status": "{status.value}"}}'.encode("ascii")
    reason = "OK" if status_code == 200 else "Service Unavailable"
    head = (
        f"HTTP/1.1 {status_code} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    return head + body


_NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


async def _read_request_path(reader: asyncio.StreamReader) -> str | None:
    """Reads just enough of the request to route it: the request line, then
    every header line up to the blank line that ends them -- never a body
    (`GET` is the only method this listener ever needs to answer). Returns
    `None` on a malformed/overlong/stalled request, which `_handle()` turns
    into simply closing the connection -- never an exception escaping to
    the server's own connection callback."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT_SECONDS)
        if not request_line:
            return None
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT_SECONDS)
            if line in (b"\r\n", b"\n", b""):
                break
    except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None

    parts = request_line.split()
    return parts[1].decode("ascii", errors="replace") if len(parts) >= 2 else None


async def _handle(
    state: HealthState, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        path = await _read_request_path(reader)
        if path == "/healthz":
            response = _response(200, HealthStatus.HEALTHY)
        elif path == "/readyz":
            response = (
                _response(200, HealthStatus.HEALTHY)
                if state.is_ready
                else _response(503, HealthStatus.UNHEALTHY)
            )
        elif path is None:
            return
        else:
            response = _NOT_FOUND
        writer.write(response)
        await writer.drain()
    except (ConnectionError, OSError):
        # A probe that disconnects mid-response is the caller's own
        # business, never a reason for this listener to log or retry --
        # see this module's own "liveness is unconditional" doctrine.
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()


async def serve_health_http(state: HealthState, *, host: str, port: int) -> asyncio.Server:
    """Start the listener. Returns the running `asyncio.Server` -- callers
    stop it with `server.close()` followed by `await server.wait_closed()`,
    the same pattern `scripts/run_call_runtime.py`'s own
    `_FreeSwitchTransport` already uses for its media listener."""

    async def _on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle(state, reader, writer)

    server = await asyncio.start_server(_on_connect, host=host, port=port, limit=_MAX_REQUEST_BYTES)
    _logger.info("health_http.started", extra={"host": host, "port": port})
    return server
