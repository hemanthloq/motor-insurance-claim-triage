"""Metrics and tracing (A6, D5).

Prometheus metric families are declared once, here, so that the API, the worker
and the stub all emit the same names. Tracing is OpenTelemetry over OTLP/HTTP;
if no collector endpoint is configured every span helper degrades to a no-op
context manager rather than failing the request.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, Iterator

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from .config import get_settings

REGISTRY = CollectorRegistry()

_LATENCY_BUCKETS = (
    0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0, 55.0, 90.0,
)

# ---- ingestion boundary (D1) -------------------------------------------------
ingest_requests = Counter(
    "triage_ingest_requests_total",
    "FNOL submissions received at the ingestion boundary.",
    ["outcome"],  # accepted | rejected | rate_limited | duplicate
    registry=REGISTRY,
)
quarantined_records = Counter(
    "triage_quarantined_records_total",
    "Records rejected by contract validation and written to quarantine.",
    ["reason"],
    registry=REGISTRY,
)

# ---- runs (A6) ---------------------------------------------------------------
runs_started = Counter(
    "triage_runs_started_total", "Agent runs started.", registry=REGISTRY
)
runs_finished = Counter(
    "triage_runs_finished_total",
    "Agent runs that reached a terminal state.",
    ["status"],  # completed | escalated | failed | timeout
    registry=REGISTRY,
)
run_latency = Histogram(
    "triage_run_duration_seconds",
    "Submission-to-final-result latency for a complete run (the D6 SLO metric).",
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
run_steps = Histogram(
    "triage_run_steps",
    "Graph steps executed per run.",
    buckets=(1, 2, 3, 4, 5, 6, 8, 10, 12, 14, 18, 24),
    registry=REGISTRY,
)
run_tool_calls = Histogram(
    "triage_run_tool_calls",
    "Tool invocations per run.",
    buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 16),
    registry=REGISTRY,
)
queue_depth = Gauge(
    "triage_queue_depth", "Jobs waiting in the execution queue.", registry=REGISTRY
)
inflight_runs = Gauge(
    "triage_inflight_runs", "Runs currently executing in this worker.", registry=REGISTRY
)

# ---- per-agent / per-step (A6) ----------------------------------------------
agent_step_latency = Histogram(
    "triage_agent_step_duration_seconds",
    "Latency of one graph step, labelled by the agent that owns it.",
    ["agent", "step"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
agent_step_outcome = Counter(
    "triage_agent_step_total",
    "Graph steps executed, by agent and outcome.",
    ["agent", "step", "outcome"],
    registry=REGISTRY,
)
agent_task_success = Counter(
    "triage_agent_task_total",
    "Per-agent task success rate numerator and denominator.",
    ["agent", "result"],  # success | failure
    registry=REGISTRY,
)

# ---- tools -------------------------------------------------------------------
tool_calls = Counter(
    "triage_tool_calls_total", "Tool invocations.", ["tool", "outcome"], registry=REGISTRY
)
tool_latency = Histogram(
    "triage_tool_duration_seconds",
    "Tool call latency.",
    ["tool"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

# ---- model provider (A5, L5) -------------------------------------------------
llm_calls = Counter(
    "triage_llm_calls_total",
    "Model calls by provider and outcome.",
    ["provider", "outcome"],
    registry=REGISTRY,
)
llm_latency = Histogram(
    "triage_llm_duration_seconds",
    "Model call latency.",
    ["provider"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
llm_retries = Counter(
    "triage_llm_retries_total", "Model call retries.", ["provider"], registry=REGISTRY
)
circuit_state = Gauge(
    "triage_circuit_state",
    "Provider circuit breaker: 0 closed, 1 half-open, 2 open.",
    ["provider"],
    registry=REGISTRY,
)
llm_tokens = Counter(
    "triage_llm_tokens_total",
    "Tokens consumed, attributed to the step that spent them.",
    ["step", "kind"],  # prompt | completion
    registry=REGISTRY,
)
llm_cost = Counter(
    "triage_llm_cost_usd_total",
    "Model spend in USD at the price in force when the call was made.",
    ["step"],
    registry=REGISTRY,
)

# ---- verification and routing ------------------------------------------------
verification_outcome = Counter(
    "triage_verification_total",
    "Self-verification results (A3).",
    ["outcome"],  # verified | unsupported | retry | escalate
    registry=REGISTRY,
)
routing_decisions = Counter(
    "triage_routing_total",
    "Final routing decisions.",
    ["queue"],  # fast_track | surveyor | investigation | human_escalation
    registry=REGISTRY,
)
guardrail_events = Counter(
    "triage_guardrail_events_total",
    "Guardrail activations (L4).",
    ["kind", "surface"],
    registry=REGISTRY,
)

# ---- retrieval and cache -----------------------------------------------------
retrieval_latency = Histogram(
    "triage_retrieval_duration_seconds",
    "Vector and lexical search latency.",
    ["strategy"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    registry=REGISTRY,
)
cache_events = Counter(
    "triage_cache_events_total",
    "Cache hits and misses (D7).",
    ["layer", "event"],
    registry=REGISTRY,
)
rate_limit_events = Counter(
    "triage_rate_limit_events_total",
    "Requests shed by the per-client rate limiter (D7).",
    ["client"],
    registry=REGISTRY,
)

# ---- corpus freshness (D2) ---------------------------------------------------
index_chunks = Gauge(
    "triage_index_chunks", "Chunks currently in the index.", registry=REGISTRY
)
index_documents = Gauge(
    "triage_index_documents",
    "Source documents currently in the index.",
    registry=REGISTRY,
)
index_built_at = Gauge(
    "triage_index_built_timestamp_seconds",
    "Unix time at which the index was last built.",
    registry=REGISTRY,
)
index_age = Gauge(
    "triage_index_age_seconds",
    "Data freshness: age of the current index.",
    registry=REGISTRY,
)
ingest_throughput = Gauge(
    "triage_ingest_chunks_per_second",
    "Throughput of the last corpus ingestion run.",
    registry=REGISTRY,
)

# ---- feedback (L6) -----------------------------------------------------------
feedback_events = Counter(
    "triage_feedback_total", "Caller feedback.", ["verdict"], registry=REGISTRY
)
live_quality_score = Gauge(
    "triage_live_quality_score",
    "Rolling automatic quality score over sampled live runs (L6).",
    ["metric"],
    registry=REGISTRY,
)
service_up = Gauge(
    "triage_service_up", "1 while the process is serving.", ["role"], registry=REGISTRY
)
service_start_time = Gauge(
    "triage_service_start_timestamp_seconds",
    "Process start time; container uptime is derived from this (A6).",
    ["role"],
    registry=REGISTRY,
)


def mark_service_up(role: str) -> None:
    service_up.labels(role=role).set(1)
    service_start_time.labels(role=role).set(time.time())


def render_metrics() -> bytes:
    return generate_latest(REGISTRY)


# ---- tracing -----------------------------------------------------------------
_tracer: Any = None


def setup_tracing(role: str, app: Any = None) -> None:
    """Wire OTLP/HTTP export to Jaeger. Silently a no-op when unavailable."""
    global _tracer
    settings = get_settings()
    if not settings.tracing_enabled or not settings.otel_exporter_otlp_endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": f"{settings.otel_service_name}-{role}",
                "service.namespace": settings.service_name,
                "deployment.environment": settings.environment,
            }
        )
        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("claimtriage")

        with contextlib.suppress(Exception):
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
        if app is not None:
            with contextlib.suppress(Exception):
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

                FastAPIInstrumentor.instrument_app(app)
    except Exception:  # tracing must never take the service down
        _tracer = None


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


def current_trace_id() -> str | None:
    if _tracer is None:
        return None
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.trace_id:
            return format(ctx.trace_id, "032x")
    except Exception:
        return None
    return None
