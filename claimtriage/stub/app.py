"""The `llm-stub` service (D6).

An OpenAI-compatible endpoint that answers in a fixed, realistic time without
touching a provider. Pointing the worker at it is three environment variables,
and nothing else about the system changes -- the same adapter, the same
connection pool, the same timeout and retry logic. That is the whole point: the
load test has to measure our system, and a stub that is reached by a different
code path would measure something we do not ship.

It also carries the failure-injection controls used by demonstration item 8:

    POST /admin/outage      every subsequent call returns 503
    POST /admin/recover     back to normal
    POST /admin/latency     set the response delay in milliseconds
    POST /admin/failure     set a failure probability in [0, 1]
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel

from ..config import get_settings
from ..llm.stub_logic import detect_step, openai_envelope
from ..logging_setup import configure_logging, get_logger

log = get_logger("llm-stub")
SERVICE_ROLE = "llm-stub"

REGISTRY = CollectorRegistry()
stub_requests = Counter(
    "stub_requests_total", "Stub completions served.", ["step", "outcome"], registry=REGISTRY
)
stub_latency = Histogram(
    "stub_duration_seconds",
    "Stub response latency, including the injected delay.",
    buckets=(0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 2.0, 5.0),
    registry=REGISTRY,
)
stub_outage = Gauge("stub_outage", "1 while the stub is simulating an outage.", registry=REGISTRY)
stub_inflight = Gauge("stub_inflight", "Concurrent requests in the stub.", registry=REGISTRY)


class _State:
    def __init__(self) -> None:
        settings = get_settings()
        self.latency_ms = settings.stub_latency_ms
        self.jitter_ms = settings.stub_latency_jitter_ms
        self.failure_rate = settings.stub_failure_rate
        self.outage = False


state = _State()
app = FastAPI(title="LLM stub", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    configure_logging(SERVICE_ROLE)
    stub_outage.set(0)
    log.info(
        "stub_started",
        latency_ms=state.latency_ms,
        jitter_ms=state.jitter_ms,
        failure_rate=state.failure_rate,
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    started = time.perf_counter()
    stub_inflight.inc()
    try:
        body = await request.json()
        prompt = "\n".join(
            str(m.get("content", "")) for m in (body.get("messages") or []) if isinstance(m, dict)
        )
        step = detect_step(prompt)

        if state.outage:
            stub_requests.labels(step=step, outcome="outage").inc()
            return JSONResponse(
                status_code=503,
                content={"error": {"message": "stub: simulated provider outage", "type": "server_error"}},
            )

        delay = (state.latency_ms + random.uniform(0, state.jitter_ms)) / 1000.0
        # asyncio.sleep, not time.sleep: the stub must stay concurrent under
        # Locust or it becomes the bottleneck the load test is meant to find.
        await asyncio.sleep(delay)

        if state.failure_rate and random.random() < state.failure_rate:
            stub_requests.labels(step=step, outcome="injected_failure").inc()
            return JSONResponse(
                status_code=503,
                content={"error": {"message": "stub: injected failure", "type": "server_error"}},
            )

        envelope = openai_envelope(prompt, model=str(body.get("model") or "stub-1"))
        stub_requests.labels(step=step, outcome="success").inc()
        return envelope
    finally:
        stub_inflight.dec()
        stub_latency.observe(time.perf_counter() - started)


@app.get("/v1/models")
def models() -> Any:
    return {"object": "list", "data": [{"id": "stub-1", "object": "model", "owned_by": "local"}]}


class LatencySetting(BaseModel):
    latency_ms: int = 350
    jitter_ms: int = 150


class FailureSetting(BaseModel):
    failure_rate: float = 0.0


@app.post("/admin/outage")
def begin_outage() -> Any:
    state.outage = True
    stub_outage.set(1)
    log.warning("stub_outage_started")
    return {"outage": True}


@app.post("/admin/recover")
def end_outage() -> Any:
    state.outage = False
    stub_outage.set(0)
    log.info("stub_outage_ended")
    return {"outage": False}


@app.post("/admin/latency")
def set_latency(setting: LatencySetting) -> Any:
    state.latency_ms = max(0, setting.latency_ms)
    state.jitter_ms = max(0, setting.jitter_ms)
    log.info("stub_latency_set", latency_ms=state.latency_ms, jitter_ms=state.jitter_ms)
    return {"latency_ms": state.latency_ms, "jitter_ms": state.jitter_ms}


@app.post("/admin/failure")
def set_failure(setting: FailureSetting) -> Any:
    state.failure_rate = min(1.0, max(0.0, setting.failure_rate))
    log.info("stub_failure_rate_set", failure_rate=state.failure_rate)
    return {"failure_rate": state.failure_rate}


@app.get("/health")
def health() -> Any:
    return {"status": "ok", "role": SERVICE_ROLE, "outage": state.outage}


@app.get("/metrics", include_in_schema=False)
def metrics() -> Any:
    return PlainTextResponse(
        generate_latest(REGISTRY).decode("utf-8"), media_type="text/plain; version=0.0.4"
    )
