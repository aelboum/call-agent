"""`voiceagent.metrics` (Phase 2.14): instruments record correctly, labels
stay bounded, and a telemetry failure never propagates.

**One `MeterProvider` for the whole module, not one per test.**
OpenTelemetry's own metrics API refuses to install a second real
`MeterProvider` once one has been installed in a process
(`opentelemetry.metrics.set_meter_provider()`'s own "Overriding of current
MeterProvider is not allowed" behavior) -- `configure_metrics(force=True)`
can only make *this module's* idempotency guard skip, it cannot make the
underlying OTel API accept a second install. Every counter here is also
cumulative by default, so a fresh `InMemoryMetricReader` per test would
still read whatever the first-installed provider is tracking, not a clean
slate. Each test below therefore uses attribute values (`outcome`,
`operation`, `worker`, ...) that no other test in this module reuses, so its
own data point is unambiguous regardless of what ran before it -- the same
"bounded, distinguishable label" property the instruments themselves rely
on in production.
"""

from __future__ import annotations

import logging

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from voiceagent import metrics as m


@pytest.fixture(scope="module")
def reader():
    reader = InMemoryMetricReader()
    m.configure_metrics(reader=reader, force=True)
    return reader


def _collected_metrics(reader: InMemoryMetricReader) -> dict[str, list]:
    data = reader.get_metrics_data()
    if data is None:
        return {}
    out: dict[str, list] = {}
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                out.setdefault(metric.name, []).extend(metric.data.data_points)
    return out


def _point_with_attributes(points, **attrs):
    for point in points:
        if dict(point.attributes) == attrs:
            return point
    return None


def test_record_call_started_increments_counter(reader) -> None:
    before = _collected_metrics(reader).get("voiceagent.calls.started", [])
    before_value = before[0].value if before else 0
    m.record_call_started()
    after = _collected_metrics(reader)["voiceagent.calls.started"]
    assert after[0].value == before_value + 1


def test_record_call_completed_records_count_and_duration(reader) -> None:
    m.record_call_completed(outcome="completed", duration_seconds=12.5)
    collected = _collected_metrics(reader)

    completed = _point_with_attributes(collected["voiceagent.calls.completed"], outcome="completed")
    assert completed is not None
    assert completed.value == 1

    duration = _point_with_attributes(collected["voiceagent.calls.duration"], outcome="completed")
    assert duration is not None
    assert duration.sum == 12.5


def test_record_call_completed_never_records_a_negative_duration(reader) -> None:
    """A clock oddity (or a caller bug) must not produce a nonsensical
    negative-duration histogram point."""
    m.record_call_completed(outcome="cancelled", duration_seconds=-5.0)
    collected = _collected_metrics(reader)
    duration = _point_with_attributes(collected["voiceagent.calls.duration"], outcome="cancelled")
    assert duration is not None
    assert duration.sum == 0.0


def test_provider_operation_labels_are_exactly_the_bounded_set(reader) -> None:
    """Brief section 6: bounded cardinality. The exported attribute set for
    a successful provider operation must be exactly `{provider_family,
    operation, outcome}` -- never a call id, tenant id, phone number, or
    transcript fragment."""
    m.record_provider_operation("stt", "transcribe", "success", 0.42)
    collected = _collected_metrics(reader)
    op = _point_with_attributes(
        collected["voiceagent.provider.operations"],
        provider_family="stt",
        operation="transcribe",
        outcome="success",
    )
    assert op is not None
    assert op.value == 1


def test_provider_operation_failure_adds_only_error_category(reader) -> None:
    m.record_provider_operation("llm", "stream_turn", "failure", 1.1, error_category="rate_limit")
    collected = _collected_metrics(reader)
    op = _point_with_attributes(
        collected["voiceagent.provider.operations"],
        provider_family="llm",
        operation="stream_turn",
        outcome="failure",
        error_category="rate_limit",
    )
    assert op is not None
    # No stray attribute beyond the four expected keys.
    assert set(dict(op.attributes)) == {
        "provider_family",
        "operation",
        "outcome",
        "error_category",
    }


def test_worker_tick_labels_never_include_a_tenant_id(reader) -> None:
    m.record_worker_tick("followups", claimed=3, failed=1, tenants_errored=1, duration_seconds=0.2)
    collected = _collected_metrics(reader)
    tick = _point_with_attributes(collected["voiceagent.workers.tick_duration"], worker="followups")
    assert tick is not None
    assert set(dict(tick.attributes)) == {"worker"}


def test_worker_tick_omits_zero_counters(reader) -> None:
    """No claimed jobs and no failures this tick for this specific worker
    label -- `jobs_claimed`/`jobs_failed` should record no data point with
    `worker="call_intelligence"`, only the tick-duration histogram."""
    m.record_worker_tick(
        "call_intelligence", claimed=0, failed=0, tenants_errored=0, duration_seconds=0.05
    )
    collected = _collected_metrics(reader)
    assert (
        _point_with_attributes(
            collected.get("voiceagent.workers.jobs_claimed", []), worker="call_intelligence"
        )
        is None
    )
    assert (
        _point_with_attributes(
            collected.get("voiceagent.workers.jobs_failed", []), worker="call_intelligence"
        )
        is None
    )
    assert (
        _point_with_attributes(
            collected["voiceagent.workers.tick_duration"], worker="call_intelligence"
        )
        is not None
    )


def test_telemetry_failure_is_swallowed_not_raised(monkeypatch, caplog) -> None:
    """Brief section 16: a telemetry backend/instrument failure must never
    propagate into the call path that triggered it."""

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated exporter failure")

    monkeypatch.setattr(m, "_calls_started", type("Boom", (), {"add": _boom})())
    with caplog.at_level(logging.WARNING):
        m.record_call_started()  # must not raise
    assert "metrics recording failed" in caplog.text


def test_configure_metrics_without_force_does_not_replace_the_installed_provider(reader) -> None:
    """A second `configure_metrics()` call with no `force` must be a no-op
    on the already-installed provider -- metrics recorded afterward still
    land in the same `reader` this module's fixture installed."""
    m.configure_metrics()  # no force
    before = _collected_metrics(reader).get("voiceagent.calls.started", [])
    before_value = before[0].value if before else 0
    m.record_call_started()
    after = _collected_metrics(reader)["voiceagent.calls.started"]
    assert after[0].value == before_value + 1
