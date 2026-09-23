"""Product-owned metrics (Phase 2.14 brief sections 6-7).

`infra.observability` builds structured logging and tracing but explicitly
does not build metrics: its own module docstring records that as "out of
scope for this phase ... revisit when a Core module actually has something
to measure" (`infra/observability/__init__.py`). Phase 2.14 is that
revisit, at the product layer -- never a change to SaaS-OS (ADR-0001).
This module is built directly on the OpenTelemetry metrics SDK that
`infra.observability`'s own tracing setup already pulls in transitively (no
new dependency; `pyproject.toml` is untouched -- confirmed importable at the
pinned SaaS-OS commit).

**`configure_metrics()` mirrors `infra.observability.otel.configure_tracing()`'s
own shape** (idempotent, a `console`/`none` exporter, an explicit override for
tests) but is called from nowhere in this module or in `voiceagent.api.app`:
`build_app()`'s own tested invariant is "no I/O at import or build time," and
starting a `PeriodicExportingMetricReader` spawns a background export thread,
which is exactly the kind of build-time side effect that invariant forbids.
`configure_tracing()`/`configure_logging()` are called from the platform's own
lifespan (SaaS-OS, off-limits) -- this product has no equivalent real-process
entrypoint yet for either the HTTP API or the call-runtime process (`scripts/`
holds only `bootstrap_rbac.py`), so `configure_metrics()` is offered as the
same kind of seam, for whichever future entrypoint wires up a real exporter,
and called directly by tests that need to assert on recorded values.

**Instruments are created at import time, before `configure_metrics()` ever
runs**, exactly like `voiceagent.observability.get_tracer()`'s own usage
already relies on: `opentelemetry.metrics.get_meter()` returns a proxy that
defers to whatever `MeterProvider` is installed at *record* time, not at
*creation* time -- the OTel API/SDK split is designed around instrumentation
code running before an application decides how (or whether) to export. A
call recorded before `configure_metrics()` ever runs is simply recorded by
the no-op default meter -- never an error.

**Bounded cardinality is the one rule every instrument here exists to
enforce** (brief section 6: "avoid metric explosion"). No call id, tenant
id, phone number, transcript, tool id, or other user-controlled string is
ever attached as a label -- only literal, closed-set values (`ProviderFamily`,
`Outcome`, a handful of fixed operation names per call site). Where an
unbounded value matters for debugging, it belongs in a structured log line
(`voiceagent.observability`) or a span attribute, never a metric label.

**Best-effort, never call-blocking** (brief section 16). Every `record_*`
function below swallows and logs any exception the instrument call itself
raises, rather than letting a telemetry failure reach the call/tool/worker
path that called it. This is a defensive backstop, not a workaround for a
known slow path: an OTel instrument's `.add()`/`.record()` call is in-process
and non-blocking by design; only a configured exporter does I/O, and it does
so off the call path, on `PeriodicExportingMetricReader`'s own background
thread.
"""

from __future__ import annotations

import logging
from typing import Final, Literal

from infra.observability.config import ObservabilityConfig, get_observability_config
from opentelemetry import metrics as _otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import (
    DEPLOYMENT_ENVIRONMENT,
    SERVICE_NAME,
    SERVICE_VERSION,
    Resource,
)

__all__ = [
    "Outcome",
    "ProviderFamily",
    "WorkerName",
    "configure_metrics",
    "record_call_completed",
    "record_call_setup_latency",
    "record_call_started",
    "record_call_teardown",
    "record_provider_operation",
    "record_reconciliation",
    "record_runtime_call_startup_failure",
    "record_runtime_shutdown",
    "record_stuck_call_cancellation",
    "record_stuck_call_detected",
    "record_teardown_timeout",
    "record_tool_execution",
    "record_worker_tick",
]

_logger = logging.getLogger(__name__)

#: Bounded provider families -- never a vendor name (brief section 6: "Do
#: not use ... arbitrary model names or user-controlled strings as unbounded
#: labels").
ProviderFamily = Literal["stt", "llm", "tts", "telephony", "media"]

#: Bounded operation outcomes, shared by every `record_provider_operation()`/
#: `record_tool_execution()` call site.
Outcome = Literal["success", "failure", "timeout", "cancelled", "denied"]

#: Bounded worker identities (brief section 14: FollowUpWorker, the call AI
#: analysis worker -- no others exist).
WorkerName = Literal["followups", "call_intelligence"]

_configured = False


def _build_resource(config: ObservabilityConfig) -> Resource:
    return Resource.create(
        {
            SERVICE_NAME: config.service_name,
            SERVICE_VERSION: config.version,
            DEPLOYMENT_ENVIRONMENT: config.environment,
            "deployment.id": config.deployment_id,
        }
    )


def _build_reader(config: ObservabilityConfig) -> MetricReader | None:
    if not config.enabled or config.exporter == "none":
        return None
    if config.exporter == "console":
        return PeriodicExportingMetricReader(ConsoleMetricExporter())
    # config.__post_init__ already validates this; unreachable in practice.
    raise AssertionError(f"unhandled exporter: {config.exporter!r}")


def configure_metrics(
    config: ObservabilityConfig | None = None,
    *,
    reader: MetricReader | None = None,
    force: bool = False,
) -> MeterProvider:
    """Build and install the global `MeterProvider`.

    `config` defaults to `get_observability_config()` -- the identical
    configuration `configure_tracing()` already reads, so metrics and traces
    are always enabled/disabled together rather than by a second env var
    (brief section 25: "do not add environment variables unnecessarily").
    `reader` overrides exporter selection entirely -- tests pass an
    `InMemoryMetricReader` (or any `MetricReader`) to assert on recorded
    values. `force=True` reconfigures even if already configured (test-only,
    mirroring `configure_tracing()`'s own `force` parameter).
    """
    global _configured

    resolved_config = config or get_observability_config()
    resolved_reader = reader if reader is not None else _build_reader(resolved_config)
    provider = MeterProvider(
        resource=_build_resource(resolved_config),
        metric_readers=[resolved_reader] if resolved_reader is not None else [],
    )

    if not _configured or force:
        _otel_metrics.set_meter_provider(provider)
        _configured = True

    return provider


_meter = _otel_metrics.get_meter("voiceagent")

_calls_started: Final = _meter.create_counter(
    "voiceagent.calls.started", unit="1", description="Calls that began execution."
)
_calls_completed: Final = _meter.create_counter(
    "voiceagent.calls.completed",
    unit="1",
    description="Calls that reached a terminal status, by outcome.",
)
_call_duration: Final = _meter.create_histogram(
    "voiceagent.calls.duration", unit="s", description="Total call duration, start to teardown."
)
_call_setup_latency: Final = _meter.create_histogram(
    "voiceagent.calls.setup_latency",
    unit="s",
    description="Time from call task start to the engine session starting.",
)
_call_teardown_latency: Final = _meter.create_histogram(
    "voiceagent.calls.teardown_latency",
    unit="s",
    description="Time spent in call teardown (engine close, media detach, finalization).",
)

_runtime_call_startup_failures: Final = _meter.create_counter(
    "voiceagent.runtime.call_startup_failures",
    unit="1",
    description="Call tasks that raised before or during teardown, by error category.",
)
_runtime_shutdown_duration: Final = _meter.create_histogram(
    "voiceagent.runtime.shutdown_duration", unit="s", description="CallRuntime.shutdown() duration."
)
_runtime_teardown_timeouts: Final = _meter.create_counter(
    "voiceagent.runtime.teardown_timeouts",
    unit="1",
    description="cancel_call() waits that exceeded cancel_timeout_seconds -- the call's own "
    "teardown keeps running in the background, uncounted here a second time.",
)
_runtime_stuck_call_detected: Final = _meter.create_counter(
    "voiceagent.runtime.stuck_calls_detected",
    unit="1",
    description="Non-terminal calls found past the configured stuck-call threshold.",
)
_runtime_stuck_call_cancellations: Final = _meter.create_counter(
    "voiceagent.runtime.stuck_call_cancellations",
    unit="1",
    description="cancel_call() invocations issued by stuck-call handling.",
)
_runtime_reconciliation: Final = _meter.create_counter(
    "voiceagent.runtime.reconciliation_repairs",
    unit="1",
    description="CallSessions reconciled after a runtime heartbeat expired, by result.",
)

_provider_operations: Final = _meter.create_counter(
    "voiceagent.provider.operations", unit="1", description="Provider operations, by outcome."
)
_provider_operation_latency: Final = _meter.create_histogram(
    "voiceagent.provider.operation_latency", unit="s", description="Provider operation latency."
)

_tool_executions: Final = _meter.create_counter(
    "voiceagent.tools.executions", unit="1", description="Tool Gateway executions, by outcome."
)
_tool_execution_latency: Final = _meter.create_histogram(
    "voiceagent.tools.execution_latency",
    unit="s",
    description="Tool Gateway handler execution latency.",
)

_worker_jobs_claimed: Final = _meter.create_counter(
    "voiceagent.workers.jobs_claimed", unit="1", description="Jobs a worker tick claimed and ran."
)
_worker_jobs_failed: Final = _meter.create_counter(
    "voiceagent.workers.jobs_failed", unit="1", description="Worker jobs that raised."
)
_worker_tenants_errored: Final = _meter.create_counter(
    "voiceagent.workers.tenants_errored",
    unit="1",
    description="Per-tenant ticks that raised, isolated from other tenants.",
)
_worker_tick_duration: Final = _meter.create_histogram(
    "voiceagent.workers.tick_duration", unit="s", description="One poll_once() tick's duration."
)


def _safe(event: str, fn) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 -- failure isolation (brief section 16):
        # a telemetry backend/instrument failure must never propagate into
        # the call/tool/worker path that triggered it.
        _logger.warning("metrics recording failed for %s", event, exc_info=True)


def record_call_started() -> None:
    _safe("call.started", lambda: _calls_started.add(1))


def record_call_completed(*, outcome: str, duration_seconds: float) -> None:
    """`outcome` is a bounded, small set of terminal call statuses
    (`completed`/`failed`/`interrupted`/`authorization_denied`) -- never a
    free-form error message."""

    def _record() -> None:
        _calls_completed.add(1, {"outcome": outcome})
        _call_duration.record(max(duration_seconds, 0.0), {"outcome": outcome})

    _safe("call.completed", _record)


def record_call_setup_latency(duration_seconds: float) -> None:
    _safe("call.setup_latency", lambda: _call_setup_latency.record(max(duration_seconds, 0.0)))


def record_call_teardown(*, duration_seconds: float, outcome: str) -> None:
    _safe(
        "call.teardown",
        lambda: _call_teardown_latency.record(max(duration_seconds, 0.0), {"outcome": outcome}),
    )


def record_runtime_call_startup_failure(*, error_category: str) -> None:
    _safe(
        "runtime.call_startup_failure",
        lambda: _runtime_call_startup_failures.add(1, {"error_category": error_category}),
    )


def record_runtime_shutdown(duration_seconds: float) -> None:
    _safe("runtime.shutdown", lambda: _runtime_shutdown_duration.record(max(duration_seconds, 0.0)))


def record_teardown_timeout() -> None:
    _safe("runtime.teardown_timeout", lambda: _runtime_teardown_timeouts.add(1))


def record_stuck_call_detected(*, phase: str) -> None:
    """`phase` is one of the bounded `voiceagent.runtime.stuck_calls` phases
    (`startup`/`active`/`teardown`)."""
    _safe(
        "runtime.stuck_call_detected",
        lambda: _runtime_stuck_call_detected.add(1, {"phase": phase}),
    )


def record_stuck_call_cancellation() -> None:
    _safe("runtime.stuck_call_cancellation", lambda: _runtime_stuck_call_cancellations.add(1))


def record_reconciliation(*, result: Literal["stale", "repaired"]) -> None:
    _safe("runtime.reconciliation", lambda: _runtime_reconciliation.add(1, {"result": result}))


def record_provider_operation(
    provider_family: ProviderFamily,
    operation: str,
    outcome: Outcome,
    duration_seconds: float,
    *,
    error_category: str | None = None,
) -> None:
    """`operation` is a short, fixed name chosen by the one call site per
    provider family (e.g. `"stream_turn"`, `"synthesize"`, `"transcribe"`,
    one of the ten ESL command names) -- never a vendor SDK method name or
    anything derived from request content."""
    attributes = {"provider_family": provider_family, "operation": operation, "outcome": outcome}
    if error_category is not None:
        # Only attached on a non-success outcome -- a bounded value already
        # (an `EngineErrorCode` member, or this product's own error taxonomy
        # category), never included on the (far more frequent) success path,
        # so it adds no cardinality to the common case.
        attributes = {**attributes, "error_category": error_category}

    def _record() -> None:
        _provider_operations.add(1, attributes)
        _provider_operation_latency.record(max(duration_seconds, 0.0), attributes)

    _safe("provider.operation", _record)


def record_tool_execution(*, outcome: str, duration_seconds: float) -> None:
    """`outcome` mirrors `voiceagent.tools.gateway`'s own bounded status
    values (`denied`/`validation_failed`/`succeeded`/`failed`/`timed_out`/
    `cancelled`/`duplicate`) -- never a tool id or handler-specific detail."""

    def _record() -> None:
        _tool_executions.add(1, {"outcome": outcome})
        _tool_execution_latency.record(max(duration_seconds, 0.0), {"outcome": outcome})

    _safe("tool.execution", _record)


def record_worker_tick(
    worker: WorkerName, *, claimed: int, failed: int, tenants_errored: int, duration_seconds: float
) -> None:
    def _record() -> None:
        attributes = {"worker": worker}
        if claimed:
            _worker_jobs_claimed.add(claimed, attributes)
        if failed:
            _worker_jobs_failed.add(failed, attributes)
        if tenants_errored:
            _worker_tenants_errored.add(tenants_errored, attributes)
        _worker_tick_duration.record(max(duration_seconds, 0.0), attributes)

    _safe("worker.tick", _record)
