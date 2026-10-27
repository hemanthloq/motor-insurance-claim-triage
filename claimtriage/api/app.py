"""The HTTP surface (F1, F8, A4, A8, D1, D5, D7, L6).

Asynchronous by construction
----------------------------
A run makes several model calls and takes tens of seconds. Holding an HTTP
connection open for that is wrong for three separate reasons, and only the
first is about elegance:

1. A claims intake sits behind a call-centre application and a mobile app.
   Neither can block a user-facing thread for forty seconds.
2. A synchronous handler couples the client's timeout to ours. The caller gives
   up at 30s, we carry on for another 20, and the work is done but nobody has
   the answer.
3. Scaling. With the work on a queue, `--scale worker=3` adds capacity without
   touching the API tier, and a worker crash loses one job rather than one
   HTTP connection and its result.

So: POST returns 202 with a run identifier immediately, the caller polls
`/v1/runs/{id}`, and an optional `X-Callback-Url` header gets a POST when the
run finishes. Polling is the primary mechanism because it needs nothing of the
caller -- no reachable endpoint, no retry handling, no replay protection -- and
because a claims handler UI is already polling a work queue. The callback is
there for the batch integrations that prefer it.

Nothing here starts an agent. The handler validates, quarantines or enqueues.
That separation is what makes "a malformed request must not start an agent run"
checkable by reading one function.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import get_settings
from ..contracts import CONTRACT_VERSION
from ..contracts.models import (
    FeedbackRequest,
    FNOLRequest,
    QuarantineRecord,
    RunAcceptedResponse,
    RunStatus,
    RunStatusResponse,
    TriageResult,
    ValidationIssue,
)
from ..correlation import (
    CORRELATION_HEADER,
    new_correlation_id,
    new_run_id,
    set_correlation_id,
    set_run_id,
)
from ..guardrails import check_fnol
from ..logging_setup import configure_logging, get_logger
from ..store import (
    backend_healthy,
    claim_idempotency,
    current_queue_depth,
    enqueue,
    load_quarantine,
    load_run,
    quality_samples,
    quarantine_count,
    rate_limit_check,
    recent_feedback,
    recent_quarantine,
    recent_runs,
    save_feedback,
    save_quarantine,
    save_run,
)
from ..telemetry import (
    feedback_events,
    ingest_requests,
    mark_service_up,
    quarantined_records,
    render_metrics,
    setup_tracing,
)
from ..validation import guardrail_quarantine, validate_submission

log = get_logger("api")
SERVICE_ROLE = "api"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(SERVICE_ROLE)
    settings = get_settings()
    setup_tracing(SERVICE_ROLE, app)
    mark_service_up(SERVICE_ROLE)
    log.info(
        "api_started",
        version=__version__,
        execution_mode=settings.execution_mode,
        provider=settings.llm_provider,
        prompt_set=settings.prompt_set,
    )
    yield
    log.info("api_stopping")


app = FastAPI(
    title="Motor Insurance Claim Intake and Triage",
    version=__version__,
    description=(
        "Advisory FNOL triage. Accepts a First Notice of Loss, validates it at "
        "the boundary, determines a likely coverage position from the insurer's "
        "own policy wording with clause-level citations, and routes the claim. "
        "The service never approves, rejects or values a claim."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[CORRELATION_HEADER],
)


# -----------------------------------------------------------------------------
# Middleware: correlation, access log
# -----------------------------------------------------------------------------


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    correlation_id = request.headers.get(CORRELATION_HEADER) or new_correlation_id()
    set_correlation_id(correlation_id)
    set_run_id("-")
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled_request_error", path=request.url.path)
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    response.headers[CORRELATION_HEADER] = correlation_id
    if request.url.path not in {"/metrics", "/health"}:
        log.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
    return response


def _client_id(request: Request, header_value: str | None) -> str:
    if header_value:
        return header_value[:64]
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


# -----------------------------------------------------------------------------
# Submission (F1, F2, D1, D7)
# -----------------------------------------------------------------------------


@app.post(
    "/v1/claims",
    status_code=202,
    response_model=RunAcceptedResponse,
    responses={422: {"model": QuarantineRecord}, 429: {"description": "rate limited"}},
    tags=["claims"],
)
async def submit_claim(
    request: Request,
    background: BackgroundTasks,
    x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
    x_callback_url: str | None = Header(default=None, alias="X-Callback-Url"),
) -> Any:
    from ..correlation import get_correlation_id

    settings = get_settings()
    correlation_id = get_correlation_id()
    client_id = _client_id(request, x_client_id)

    # ---- 1. rate limit, before anything expensive (D7) --------------------
    allowed, remaining, reset_in = rate_limit_check(client_id)
    if not allowed:
        ingest_requests.labels(outcome="rate_limited").inc()
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limited",
                "detail": (
                    f"{settings.rate_limit_requests} requests per "
                    f"{settings.rate_limit_window_seconds}s per client"
                ),
                "retry_after_seconds": reset_in,
                "correlation_id": correlation_id,
            },
            headers={"Retry-After": str(reset_in), "X-RateLimit-Remaining": "0"},
        )

    try:
        payload = await request.json()
    except Exception:
        ingest_requests.labels(outcome="rejected").inc()
        quarantined_records.labels(reason="malformed_json").inc()
        record = guardrail_quarantine(
            {},
            correlation_id=correlation_id,
            issues=[
                ValidationIssue(
                    field="<body>", rule="malformed_json", message="request body is not valid JSON"
                )
            ],
            client_id=client_id,
            stage="schema",
        )
        save_quarantine(record.model_dump(mode="json"))
        return JSONResponse(status_code=422, content=record.model_dump(mode="json"))

    # ---- 2. contract validation (D1) --------------------------------------
    outcome = validate_submission(payload, correlation_id=correlation_id, client_id=client_id)
    if not outcome.ok:
        assert outcome.quarantine is not None
        ingest_requests.labels(outcome="rejected").inc()
        for issue in outcome.errors:
            quarantined_records.labels(reason=issue.rule).inc()
        save_quarantine(outcome.quarantine.model_dump(mode="json"))
        log.warning(
            "submission_quarantined",
            quarantine_id=outcome.quarantine.quarantine_id,
            stage=outcome.quarantine.stage,
            issues=[i.rule for i in outcome.errors],
            agent_run_started=False,
        )
        return JSONResponse(status_code=422, content=outcome.quarantine.model_dump(mode="json"))

    fnol: FNOLRequest = outcome.fnol  # type: ignore[assignment]

    # ---- 3. guardrails on free text, still before any agent (L4) ----------
    guard = check_fnol(payload)
    if not guard.allowed:
        ingest_requests.labels(outcome="rejected").inc()
        quarantined_records.labels(reason="guardrail").inc()
        record = guardrail_quarantine(
            payload,
            correlation_id=correlation_id,
            issues=[
                ValidationIssue(
                    field="incident.description",
                    rule=f"guardrail:{f.kind}:{f.pattern}",
                    message=(
                        "the description contains content this service will not pass "
                        "to a model"
                    ),
                    observed=f.excerpt,
                )
                for f in guard.findings
                if f.action == "block"
            ],
            client_id=client_id,
            stage="guardrail",
        )
        save_quarantine(record.model_dump(mode="json"))
        log.warning(
            "submission_blocked_by_guardrail",
            quarantine_id=record.quarantine_id,
            patterns=[f.pattern for f in guard.findings],
            agent_run_started=False,
        )
        return JSONResponse(status_code=422, content=record.model_dump(mode="json"))

    # ---- 4. idempotency ---------------------------------------------------
    run_id = new_run_id()
    existing = claim_idempotency(fnol.claim_reference, run_id, settings.run_ttl_seconds)
    if existing:
        ingest_requests.labels(outcome="duplicate").inc()
        log.info("duplicate_submission", claim_reference=fnol.claim_reference, run_id=existing)
        document = load_run(existing) or {}
        return JSONResponse(
            status_code=200,
            content=RunAcceptedResponse(
                run_id=existing,
                correlation_id=document.get("correlation_id", correlation_id),
                claim_reference=fnol.claim_reference,
                status=RunStatus(document.get("status", "queued")),
                status_url=f"/v1/runs/{existing}",
                result_url=f"/v1/runs/{existing}/result",
                queued_at=datetime.fromisoformat(document["queued_at"])
                if document.get("queued_at")
                else datetime.now(timezone.utc),
                estimated_seconds=settings.slo_p95_seconds,
            ).model_dump(mode="json"),
        )

    # ---- 5. enqueue -------------------------------------------------------
    set_run_id(run_id)
    now = datetime.now(timezone.utc)
    claim = fnol.model_dump(mode="json", by_alias=False)
    warnings = [w.model_dump(mode="json") for w in outcome.warnings]
    guardrail_flags = [f.to_dict() for f in guard.findings]

    document = {
        "run_id": run_id,
        "correlation_id": correlation_id,
        "claim_reference": fnol.claim_reference,
        "client_id": client_id,
        "status": RunStatus.queued.value,
        "queued_at": now.isoformat(),
        "started_at": None,
        "completed_at": None,
        "current_step": None,
        "steps_completed": 0,
        "claim": claim,
        "warnings": warnings,
        "guardrail_flags": guardrail_flags,
        "callback_url": x_callback_url,
        "result": None,
    }
    save_run(run_id, document)

    job = {
        "run_id": run_id,
        "correlation_id": correlation_id,
        "claim_reference": fnol.claim_reference,
        "enqueued_at": now.isoformat(),
    }

    if settings.execution_mode == "inline":
        # Development convenience only: runs in this process, after the
        # response has been sent, so the contract the caller sees is identical.
        background.add_task(_execute_inline, job)
    else:
        enqueue(job)

    ingest_requests.labels(outcome="accepted").inc()
    log.info(
        "submission_accepted",
        run_id=run_id,
        claim_reference=fnol.claim_reference,
        warnings=[w.rule for w in outcome.warnings],
        queue_depth=current_queue_depth(),
    )
    return JSONResponse(
        status_code=202,
        content=RunAcceptedResponse(
            run_id=run_id,
            correlation_id=correlation_id,
            claim_reference=fnol.claim_reference,
            status=RunStatus.queued,
            status_url=f"/v1/runs/{run_id}",
            result_url=f"/v1/runs/{run_id}/result",
            queued_at=now,
            estimated_seconds=settings.slo_p95_seconds,
        ).model_dump(mode="json"),
        headers={"X-RateLimit-Remaining": str(remaining), "Location": f"/v1/runs/{run_id}"},
    )


def _execute_inline(job: dict[str, Any]) -> None:
    from ..worker.executor import process_job

    process_job(job)


# -----------------------------------------------------------------------------
# Run status and results
# -----------------------------------------------------------------------------


@app.get("/v1/runs/{run_id}", response_model=RunStatusResponse, tags=["claims"])
def run_status(run_id: str) -> Any:
    document = load_run(run_id)
    if not document:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
    queued_at = datetime.fromisoformat(document["queued_at"])
    completed_at = (
        datetime.fromisoformat(document["completed_at"]) if document.get("completed_at") else None
    )
    elapsed = ((completed_at or datetime.now(timezone.utc)) - queued_at).total_seconds()
    return RunStatusResponse(
        run_id=run_id,
        correlation_id=document.get("correlation_id", "-"),
        status=RunStatus(document["status"]),
        current_step=document.get("current_step"),
        steps_completed=int(document.get("steps_completed") or 0),
        queued_at=queued_at,
        started_at=datetime.fromisoformat(document["started_at"])
        if document.get("started_at")
        else None,
        completed_at=completed_at,
        elapsed_seconds=round(elapsed, 3),
        result_available=document.get("result") is not None,
        result_url=f"/v1/runs/{run_id}/result",
    )


@app.get("/v1/runs/{run_id}/result", response_model=TriageResult, tags=["claims"])
def run_result(run_id: str) -> Any:
    document = load_run(run_id)
    if not document:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
    if document.get("result") is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "run_not_finished",
                "status": document["status"],
                "status_url": f"/v1/runs/{run_id}",
            },
        )
    return JSONResponse(content=document["result"])


@app.get("/v1/runs/{run_id}/trace", tags=["observability"])
def run_trace(run_id: str) -> Any:
    """Reconstruct an entire run from its identifier (D5).

    Every tool call with its query and the clauses it returned, every prompt
    version, per-step latency and cost, and the verification outcome.
    """
    document = load_run(run_id)
    if not document:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
    result = document.get("result") or {}
    return {
        "run_id": run_id,
        "correlation_id": document.get("correlation_id"),
        "trace_id": result.get("trace_id"),
        "status": document.get("status"),
        "claim_reference": document.get("claim_reference"),
        "queued_at": document.get("queued_at"),
        "started_at": document.get("started_at"),
        "completed_at": document.get("completed_at"),
        "duration_seconds": result.get("duration_seconds"),
        "index_version": result.get("index_version"),
        "prompt_versions": result.get("prompt_versions"),
        "validation_warnings": document.get("warnings"),
        "guardrail_events": (document.get("guardrail_flags") or [])
        + (result.get("guardrail_events") or []),
        "steps": result.get("steps"),
        "tool_calls": document.get("tool_calls"),
        "verification": result.get("verification"),
        "citations": result.get("citations"),
        "cost": result.get("cost"),
        "degraded": result.get("degraded"),
        "degradation_path": result.get("degradation_path"),
        "worker": document.get("worker"),
    }


@app.get("/v1/runs", tags=["claims"])
def list_runs(limit: int = 25) -> Any:
    runs = recent_runs(min(max(limit, 1), 200))
    return {
        "count": len(runs),
        "runs": [
            {
                "run_id": r.get("run_id"),
                "claim_reference": r.get("claim_reference"),
                "status": r.get("status"),
                "queued_at": r.get("queued_at"),
                "completed_at": r.get("completed_at"),
                "queue": ((r.get("result") or {}).get("routing") or {}).get("queue"),
                "position": ((r.get("result") or {}).get("coverage") or {}).get("position"),
                "duration_seconds": (r.get("result") or {}).get("duration_seconds"),
            }
            for r in runs
        ],
    }


# -----------------------------------------------------------------------------
# Quarantine (D1)
# -----------------------------------------------------------------------------


@app.get("/v1/quarantine", tags=["quality"])
def list_quarantine(limit: int = 25) -> Any:
    records = recent_quarantine(min(max(limit, 1), 200))
    return {"count": quarantine_count(), "records": records}


@app.get("/v1/quarantine/{quarantine_id}", tags=["quality"])
def get_quarantine(quarantine_id: str) -> Any:
    record = load_quarantine(quarantine_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"unknown quarantine record {quarantine_id}")
    return record


# -----------------------------------------------------------------------------
# Feedback (L6)
# -----------------------------------------------------------------------------


@app.post("/v1/feedback", status_code=201, tags=["quality"])
def submit_feedback(feedback: FeedbackRequest) -> Any:
    from ..correlation import get_correlation_id

    document = load_run(feedback.run_id)
    if not document:
        raise HTTPException(status_code=404, detail=f"unknown run {feedback.run_id}")
    result = document.get("result") or {}
    record = {
        "feedback_id": f"fb-{uuid.uuid4().hex[:16]}",
        "run_id": feedback.run_id,
        "correlation_id": get_correlation_id(),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "verdict": feedback.verdict.value,
        "reason": feedback.reason,
        "reviewer": feedback.reviewer,
        "corrected_queue": feedback.corrected_queue.value if feedback.corrected_queue else None,
        "corrected_position": feedback.corrected_position.value
        if feedback.corrected_position
        else None,
        "observed_queue": (result.get("routing") or {}).get("queue"),
        "observed_position": (result.get("coverage") or {}).get("position"),
        "prompt_versions": result.get("prompt_versions"),
        "index_version": result.get("index_version"),
    }
    save_feedback(record)
    feedback_events.labels(verdict=feedback.verdict.value).inc()
    log.info("feedback_received", **{k: record[k] for k in ("feedback_id", "run_id", "verdict")})
    return record


@app.get("/v1/feedback/summary", tags=["quality"])
def feedback_summary(limit: int = 500) -> Any:
    """Aggregated caller verdicts alongside the automatic quality samples (L6)."""
    records = recent_feedback(limit)
    satisfactory = sum(1 for r in records if r.get("verdict") == "satisfactory")
    by_queue: dict[str, dict[str, int]] = {}
    for record in records:
        bucket = by_queue.setdefault(
            record.get("observed_queue") or "unknown", {"satisfactory": 0, "unsatisfactory": 0}
        )
        bucket[record.get("verdict", "unsatisfactory")] += 1

    samples = quality_samples(limit)
    metrics: dict[str, float] = {}
    if samples:
        for key in ("groundedness", "context_relevance", "faithfulness", "hallucination_rate"):
            values = [float(s["metrics"][key]) for s in samples if key in (s.get("metrics") or {})]
            if values:
                metrics[key] = round(sum(values) / len(values), 4)

    return {
        "total": len(records),
        "satisfactory": satisfactory,
        "unsatisfactory": len(records) - satisfactory,
        "satisfaction_rate": round(satisfactory / len(records), 4) if records else None,
        "by_queue": by_queue,
        "corrections": [
            {
                "run_id": r["run_id"],
                "observed_queue": r.get("observed_queue"),
                "corrected_queue": r.get("corrected_queue"),
                "reason": r.get("reason"),
            }
            for r in records
            if r.get("corrected_queue") and r.get("corrected_queue") != r.get("observed_queue")
        ][:50],
        "automatic_quality": {
            "sampled_runs": len(samples),
            "rolling_metrics": metrics,
            "series": [
                {"run_id": s["run_id"], "at": s.get("at"), **(s.get("metrics") or {})}
                for s in samples[:100]
            ],
        },
    }


# -----------------------------------------------------------------------------
# Corpus and contracts
# -----------------------------------------------------------------------------


@app.get("/v1/corpus/clause/{clause_id}", tags=["corpus"])
def get_clause(clause_id: str) -> Any:
    """Resolve a citation. The web client links every citation here."""
    from ..retrieval.index import get_index

    hit = get_index().get_clause(clause_id)
    if not hit:
        raise HTTPException(status_code=404, detail=f"unknown clause {clause_id}")
    return hit.to_dict()


@app.get("/contracts", tags=["contracts"])
def list_contracts() -> Any:
    return {
        "contract_version": CONTRACT_VERSION,
        "contracts": {
            name: f"/contracts/{name}"
            for name in ("fnol", "triage_result", "feedback", "quarantine")
        },
    }


_CONTRACT_MODELS = {
    "fnol": FNOLRequest,
    "triage_result": TriageResult,
    "feedback": FeedbackRequest,
    "quarantine": QuarantineRecord,
}


@app.get("/contracts/{name}", tags=["contracts"])
def get_contract(name: str) -> Any:
    model = _CONTRACT_MODELS.get(name)
    if model is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown contract {name}; available: {sorted(_CONTRACT_MODELS)}",
        )
    schema = model.model_json_schema()
    schema["$id"] = f"https://claim-triage.local/contracts/{name}.v{CONTRACT_VERSION}.json"
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


# -----------------------------------------------------------------------------
# Health and metrics (F8)
# -----------------------------------------------------------------------------


@app.get("/health", tags=["observability"])
def health() -> Any:
    """Liveness. Cheap, and must not depend on anything downstream."""
    return {"status": "ok", "role": SERVICE_ROLE, "version": __version__}


@app.get("/health/ready", tags=["observability"])
def readiness(response: Response) -> Any:
    """Readiness. Depends on everything this process needs to serve a request."""
    from ..retrieval.embeddings import active_backend
    from ..retrieval.index import get_index

    settings = get_settings()
    checks: dict[str, Any] = {}

    checks["state_backend"] = {"ok": backend_healthy(), "url_scheme": settings.redis_url.split("://")[0]}
    try:
        index = get_index()
        checks["corpus_index"] = {
            "ok": index.size > 0,
            "chunks": index.size,
            "backend": type(index.store).__name__,
            "embeddings": active_backend(),
        }
    except Exception as exc:
        checks["corpus_index"] = {"ok": False, "error": str(exc)[:200]}

    try:
        from ..prompts import get_registry

        checks["prompts"] = {"ok": True, "set": settings.prompt_set, "sets": get_registry().sets}
    except Exception as exc:
        checks["prompts"] = {"ok": False, "error": str(exc)[:200]}

    checks["queue"] = {"ok": True, "depth": current_queue_depth(), "mode": settings.execution_mode}

    ready = all(c.get("ok") for c in checks.values())
    if not ready:
        response.status_code = 503
    return {"ready": ready, "role": SERVICE_ROLE, "checks": checks}


@app.get("/health/corpus", tags=["observability"])
def corpus_health() -> Any:
    """Data freshness (D2): when the index was built, from what, how big."""
    from ..retrieval.index import get_index, read_index_meta

    meta = read_index_meta()
    index = get_index()
    built = meta.get("built_at_epoch")
    return {
        "index_version": meta.get("index_version"),
        "built_at": meta.get("built_at"),
        "age_seconds": round(time.time() - float(built), 1) if built else None,
        "chunks": index.size,
        "documents": [
            {
                "document_id": d.get("document_id"),
                "title": d.get("title"),
                "source_url": d.get("source_url"),
                "content_sha256": d.get("content_sha256"),
                "pages": d.get("pages"),
                "chunks": d.get("chunks"),
                "roles": d.get("roles"),
            }
            for d in (meta.get("documents") or [])
        ],
        "embedding_signature": meta.get("embedding_signature"),
        "build_duration_seconds": meta.get("build_duration_seconds"),
        "chunks_per_second": meta.get("chunks_per_second"),
    }


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return PlainTextResponse(
        content=render_metrics().decode("utf-8"), media_type="text/plain; version=0.0.4"
    )


# -----------------------------------------------------------------------------
# Admin -- for the demonstration items, not for production
# -----------------------------------------------------------------------------


@app.post("/v1/admin/circuit/{action}", tags=["admin"])
def circuit_control(action: str) -> Any:
    """Trip or reset the provider circuit breaker (demonstration item 8).

    The API tier does not call the model, so this is a demonstration aid for
    the API's own client; the worker exposes the same control on its own
    admin port. Both are behind an explicitly named admin path so that it is
    obvious this is not a production endpoint.
    """
    from ..llm import get_client

    client = get_client()
    if action == "open":
        client.breaker.force_open()
    elif action == "reset":
        client.breaker.reset()
    else:
        raise HTTPException(status_code=400, detail="action must be 'open' or 'reset'")
    return {"provider": client.provider.name, "state": client.breaker.state, "action": action}


# -----------------------------------------------------------------------------
# Web client (A8)
# -----------------------------------------------------------------------------

try:
    app.mount("/", StaticFiles(directory="/app/web", html=True), name="web")
except Exception:  # not present outside the container
    pass
