"""Structured JSON logging."""

from __future__ import annotations

import logging
import time
from typing import Any

import structlog

from llmgate.core.context import (
    api_key_id_var,
    error_var,
    model_var,
    provider_var,
    stream_var,
    usage_var,
)
from llmgate.core.schemas import Usage

_QUIET_PATHS = {"/healthz", "/"}


def configure_logging(level: str) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def log_access(
    *,
    method: str,
    path: str,
    status_code: int,
    started: float,
    scope_state: dict[str, Any] | None = None,
) -> None:
    """One line per request: identity, latency, and token usage when known."""
    usage = _usage(scope_state)
    error = _error(scope_state)
    logger = structlog.get_logger("llmgate.access")
    payload = {
        "method": method,
        "path": path,
        "status_code": status_code,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "model": model_var.get(),
        "provider": provider_var.get(),
        "stream": stream_var.get(),
        "api_key_id": api_key_id_var.get(),
        "prompt_tokens": usage.prompt_tokens if usage is not None else None,
        "completion_tokens": usage.completion_tokens if usage is not None else None,
        "total_tokens": usage.total_tokens if usage is not None else None,
        "error": error,
    }
    if path in _QUIET_PATHS:
        logger.debug("request.completed", **payload)
    else:
        logger.info("request.completed", **payload)


def _usage(scope_state: dict[str, Any] | None) -> Usage | None:
    if scope_state is not None:
        stored = scope_state.get("usage")
        if isinstance(stored, Usage):
            return stored
    return usage_var.get()


def _error(scope_state: dict[str, Any] | None) -> str | None:
    if scope_state is not None:
        stored = scope_state.get("stream_error")
        if isinstance(stored, str):
            return stored
    return error_var.get()
