"""Structured logging (D5).

Every log line is a JSON object carrying the correlation ID, the run ID and the
current agent step, so a single run can be pulled out of a multi-container log
stream with one filter.
"""

from __future__ import annotations

import logging
import sys

import structlog

from .config import get_settings
from .correlation import current_context

_configured = False


def _inject_context(_logger, _name, event_dict):
    event_dict.update(current_context())
    return event_dict


def configure_logging(role: str | None = None) -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    for noisy in ("httpx", "httpcore", "urllib3", "chromadb", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _inject_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(
        service=settings.service_name, role=role or settings.service_role
    )
    _configured = True


def get_logger(name: str = "claimtriage"):
    configure_logging()
    return structlog.get_logger(name)
