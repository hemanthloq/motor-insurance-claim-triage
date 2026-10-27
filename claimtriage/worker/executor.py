"""The worker: pulls a job, runs the graph, writes the result back (A4, A6, A7).

Scaling model
-------------
The worker is stateless. Everything it needs comes from the job (a run id), the
run store (the validated claim) and the shared index. That is what makes
`docker compose up --scale worker=3` meaningful: three replicas compete on one
Redis list, Redis hands each job to exactly one of them, and nothing has to be
partitioned.

Concurrency inside one replica is a thread pool rather than asyncio, because
the work is a mixture of blocking HTTP (the provider) and CPU (embedding a
query, scoring BM25). `WORKER_CONCURRENCY` sets the pool size. Two levers
therefore exist -- replicas and threads -- and docs/scaling.md reports what
each one actually bought.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import httpx

from .. import __version__
from ..config import get_settings
from ..contracts.models import RunStatus
from ..correlation import set_correlation_id, set_run_id, set_step
from ..logging_setup import configure_logging, get_logger
from ..store import current_queue_depth, dequeue, load_run, patch_run, record_quality_sample
from ..telemetry import live_quality_score, mark_service_up, setup_tracing, span

log = get_logger("worker")
SERVICE_ROLE = "worker"

_shutdown = threading.Event()

#: Share of completed runs scored automatically against the evaluation metrics
#: and charted over time (L6). One in five keeps the cost of the extra scoring
#: pass proportionate while still producing a usable series within an hour of
#: normal traffic.
QUALITY_SAMPLE_RATE = 0.2


def process_job(job: dict[str, Any]) -> dict[str, Any] | None:
    """Execute one run. Never raises: a worker that dies on a bad job stops the
    queue for everyone."""
    from ..agents.runner import execute_run

    run_id = job.get("run_id")
    correlation_id = job.get("correlation_id", "-")
    if not run_id:
        log.error("job_without_run_id", job=job)
        return None

    set_correlation_id(correlation_id)
    set_run_id(run_id)
    set_step("dequeued")

    document = load_run(run_id)
    if document is None:
        log.error("job_for_unknown_run", run_id=run_id)
        return None
    if document.get("status") in {RunStatus.completed.value, RunStatus.escalated.value}:
        log.info("job_already_finished", run_id=run_id, status=document["status"])
        return document.get("result")

    queued_at = document.get("queued_at")
    started_at = datetime.now(timezone.utc)
    patch_run(
        run_id,
        status=RunStatus.running.value,
        started_at=started_at.isoformat(),
        current_step="intake",
        worker={"host": os.uname().nodename if hasattr(os, "uname") else os.environ.get("HOSTNAME", "local"),
                "pid": os.getpid(), "version": __version__},
    )

    with span("worker.job", **{"run.id": run_id, "run.correlation_id": correlation_id}):
        result = execute_run(
            run_id=run_id,
            correlation_id=correlation_id,
            claim=document["claim"],
            warnings=document.get("warnings") or [],
            submitted_at=datetime.fromisoformat(queued_at) if queued_at else started_at,
        )

    payload = result.model_dump(mode="json")
    patch_run(
        run_id,
        status=result.status.value,
        completed_at=(result.completed_at or datetime.now(timezone.utc)).isoformat(),
        current_step="done",
        steps_completed=len(result.steps),
        tool_calls=result.tool_calls,
        result=payload,
    )

    _sample_quality(run_id, payload)
    _fire_callback(document.get("callback_url"), payload)
    set_step("-")
    return payload


def _sample_quality(run_id: str, payload: dict[str, Any]) -> None:
    """Score a share of live runs automatically against the evaluation metrics (L6)."""
    import random

    if random.random() > QUALITY_SAMPLE_RATE:
        return
    try:
        from ..eval.metrics import score_live_run

        metrics = score_live_run(payload)
        record_quality_sample(
            {
                "run_id": run_id,
                "at": datetime.now(timezone.utc).isoformat(),
                "metrics": metrics,
                "prompt_versions": payload.get("prompt_versions"),
                "index_version": payload.get("index_version"),
            }
        )
        for name, value in metrics.items():
            live_quality_score.labels(metric=name).set(value)
        log.info("live_run_scored", run_id=run_id, **metrics)
    except Exception as exc:
        log.warning("live_scoring_failed", run_id=run_id, error=str(exc)[:200])


def _fire_callback(url: str | None, payload: dict[str, Any]) -> None:
    if not url:
        return
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(url, json=payload)
        log.info("callback_delivered", url=url, status=response.status_code)
    except Exception as exc:
        # Best effort by design. The caller can always poll; a failed callback
        # must not fail a run whose work is already done and stored.
        log.warning("callback_failed", url=url, error=str(exc)[:200])


def _worker_loop(index: int) -> None:
    log.info("worker_thread_started", thread=index)
    while not _shutdown.is_set():
        try:
            job = dequeue(timeout=2)
        except Exception as exc:
            log.error("dequeue_failed", error=str(exc)[:200])
            time.sleep(1.0)
            continue
        if job is None:
            continue
        try:
            process_job(job)
        except Exception as exc:
            log.exception("job_failed_unexpectedly", error=str(exc), job=job)
            if job.get("run_id"):
                patch_run(
                    job["run_id"],
                    status=RunStatus.failed.value,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    error=str(exc)[:500],
                )
    log.info("worker_thread_stopped", thread=index)


def _serve_admin(port: int) -> None:
    """A tiny HTTP surface on the worker: health, metrics, circuit control.

    Prometheus must be able to scrape every service (D5), and the circuit
    breaker that matters lives in this process, not in the API -- the worker is
    what calls the model. So the worker needs a port of its own.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from ..llm import get_client
    from ..retrieval.index import get_index
    from ..store import backend_healthy
    from ..telemetry import render_metrics

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/metrics":
                self._send(200, render_metrics(), "text/plain; version=0.0.4")
            elif self.path == "/health":
                self._send(
                    200,
                    json.dumps({"status": "ok", "role": SERVICE_ROLE, "version": __version__}).encode(),
                    "application/json",
                )
            elif self.path == "/health/ready":
                try:
                    chunks = get_index().size
                except Exception:
                    chunks = 0
                ready = backend_healthy() and chunks > 0
                self._send(
                    200 if ready else 503,
                    json.dumps(
                        {
                            "ready": ready,
                            "role": SERVICE_ROLE,
                            "queue_depth": current_queue_depth(),
                            "index_chunks": chunks,
                        }
                    ).encode(),
                    "application/json",
                )
            else:
                self._send(404, b'{"error":"not found"}', "application/json")

        def do_POST(self):  # noqa: N802
            if self.path.startswith("/admin/circuit/"):
                action = self.path.rsplit("/", 1)[-1]
                client = get_client()
                if action == "open":
                    client.breaker.force_open()
                elif action == "reset":
                    client.breaker.reset()
                else:
                    self._send(400, b'{"error":"action must be open or reset"}', "application/json")
                    return
                self._send(
                    200,
                    json.dumps(
                        {"provider": client.provider.name, "state": client.breaker.state}
                    ).encode(),
                    "application/json",
                )
            else:
                self._send(404, b'{"error":"not found"}', "application/json")

        def log_message(self, *_args):  # silence the default stderr access log
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="worker-admin").start()
    log.info("worker_admin_listening", port=port)


def main() -> None:
    configure_logging(SERVICE_ROLE)
    settings = get_settings()
    setup_tracing(SERVICE_ROLE)
    mark_service_up(SERVICE_ROLE)
    _serve_admin(int(os.environ.get("WORKER_ADMIN_PORT", "9100")))

    # Warm the heavy singletons before taking work, so the first claim of the
    # day does not pay for the embedding model load.
    try:
        from ..retrieval.index import get_index

        index = get_index()
        log.info("worker_index_ready", chunks=index.size)
    except Exception as exc:
        log.error("worker_index_unavailable", error=str(exc)[:300])

    def _stop(signum, _frame):
        log.info("shutdown_signal", signal=signum)
        _shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, AttributeError):
            pass

    log.info(
        "worker_started",
        concurrency=settings.worker_concurrency,
        queue=settings.queue_name,
        provider=settings.llm_provider,
        prompt_set=settings.prompt_set,
    )
    with ThreadPoolExecutor(
        max_workers=settings.worker_concurrency, thread_name_prefix="run"
    ) as pool:
        futures = [pool.submit(_worker_loop, i) for i in range(settings.worker_concurrency)]
        for future in futures:
            future.result()
    log.info("worker_stopped")


if __name__ == "__main__":
    main()
