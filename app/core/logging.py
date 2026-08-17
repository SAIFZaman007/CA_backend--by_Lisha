"""Structured JSON logging in production, human-readable locally."""

import logging
import sys

import structlog

from app.core.config import settings


def configure_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)

    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer = (
        structlog.processors.JSONRenderer()
        if settings.is_production
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Quieten access logs; the request middleware emits richer entries.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str = "coach-auto"):
    return structlog.get_logger(name)
