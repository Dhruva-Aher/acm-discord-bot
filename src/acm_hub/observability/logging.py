"""Structured logging setup.

``console`` renders human-readable lines for local development; ``text`` (used
in Docker/production) renders one JSON object per line so a log shipper can
index the event fields directly.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO", log_format: str = "console") -> None:
    """Install the structlog pipeline and route stdlib logging through it.

    discord.py and SQLAlchemy log via the stdlib, so this also normalizes their
    output into the same stream.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Any = (
        structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        if log_format == "console"
        else structlog.processors.JSONRenderer()
    )

    # Everything goes through stdlib logging, including our own structlog calls.
    # That keeps one handler, one format and one level for the whole process,
    # and it is required by `add_logger_name`, which reads the stdlib logger.
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric_level)

    # discord.py's gateway logger is extremely chatty at DEBUG and can echo
    # session identifiers, so it stays at WARNING regardless of our level.
    logging.getLogger("discord.gateway").setLevel(max(numeric_level, logging.WARNING))
    logging.getLogger("discord.client").setLevel(max(numeric_level, logging.INFO))
    logging.getLogger("httpx").setLevel(max(numeric_level, logging.WARNING))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structured logger for ``name``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
