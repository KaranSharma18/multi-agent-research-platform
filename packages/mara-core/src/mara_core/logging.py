"""Structured logging with structlog.

Logs are key=value pairs, never free text with secrets in it. Development gets
readable console output; set LOG_JSON=true for one JSON object per line, which
is what log shippers want. Prompt text and tokens never go into logs; that is a
rule for callers, and `redact_token` helps keep it.
"""

from __future__ import annotations

import logging
import sys

import structlog

_configured = False


def configure_logging(service: str, level: str = "INFO", json_logs: bool = False) -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(stream=sys.stdout, level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")
    # Libraries that are chatty at INFO.
    for noisy in (
        "httpx",
        "httpcore",
        "mcp.client",
        "mcp.server.lowlevel",
        "mcp.server.streamable_http",
        "mcp.server.streamable_http_manager",
        "urllib3",
        "google_genai",
        "uvicorn.access",
        "a2a",
        "sse_starlette",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer(colors=False)
    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service)
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def redact_token(token: str | None) -> str:
    """Show only enough of a token to correlate it in logs."""
    if not token:
        return "<none>"
    return f"{token[:6]}…{token[-4:]}" if len(token) > 12 else "<short>"
