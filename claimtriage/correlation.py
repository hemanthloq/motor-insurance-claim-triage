"""Correlation ID propagation (D5).

One identifier follows a claim from the HTTP request that submitted it, through
the queue, into the worker, down every agent step and tool call, and out into
the logs, the metrics exemplars and the trace. It is generated at the ingestion
boundary if the caller did not supply one.
"""

from __future__ import annotations

import contextvars
import uuid

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)
_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
_step: contextvars.ContextVar[str] = contextvars.ContextVar("step", default="-")

CORRELATION_HEADER = "X-Correlation-ID"


def new_correlation_id() -> str:
    return f"cid-{uuid.uuid4().hex[:16]}"


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:16]}"


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str:
    return _correlation_id.get()


def set_run_id(value: str) -> None:
    _run_id.set(value)


def get_run_id() -> str:
    return _run_id.get()


def set_step(value: str) -> None:
    _step.set(value)


def get_step() -> str:
    return _step.get()


def current_context() -> dict[str, str]:
    return {
        "correlation_id": get_correlation_id(),
        "run_id": get_run_id(),
        "step": get_step(),
    }
