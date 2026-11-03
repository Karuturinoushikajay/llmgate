"""Structured JSON logging, Prometheus observation, and request log rows."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from llmgate.core.context import (
    api_key_id_var,
    cache_var,
    error_var,
    model_var,
    provider_var,
    request_id_var,
    route_var,
    stream_var,
    team_id_var,
    usage_var,
)
from llmgate.core.metrics import observe_request
from llmgate.core.pricing import PricingTable
from llmgate.core.schemas import Usage
from llmgate.storage.db import session_scope
from llmgate.storage.observability import append_request_log

_QUIET_PATHS = {"/healthz", "/", "/metrics"}


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


async def record_access(
    *,
    method: str,
    path: str,
    status_code: int,
    started: float,
    scope_state: dict[str, Any] | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    pricing: PricingTable | None = None,
) -> None:
    """One line per request, then metrics and an optional append-only row."""
    usage = _usage(scope_state)
    error = _error(scope_state)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    model = model_var.get()
    cache = cache_var.get()
    prompt = usage.prompt_tokens if usage is not None else None
    completion = usage.completion_tokens if usage is not None else None
    total = usage.total_tokens if usage is not None else None
    cost = None
    if pricing is not None:
        cost = pricing.estimate(
            model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache=cache,
        )
    payload = {
        "method": method,
        "path": path,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "model": model,
        "provider": provider_var.get(),
        "route": route_var.get(),
        "cache": cache,
        "stream": stream_var.get(),
        "api_key_id": api_key_id_var.get(),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "estimated_cost_usd": cost,
        "error": error,
    }
    logger = structlog.get_logger("llmgate.access")
    if path in _QUIET_PATHS:
        logger.debug("request.completed", **payload)
    else:
        logger.info("request.completed", **payload)
        observe_request(
            method=method,
            path=path,
            status_code=status_code,
            latency_seconds=latency_ms / 1000,
            model=model,
            cache=cache,
            prompt_tokens=prompt,
            completion_tokens=completion,
            estimated_cost_usd=cost,
        )
        if session_factory is not None and path.startswith("/v1/"):
            await _persist(
                session_factory,
                payload=payload,
                request_id=request_id_var.get() or "",
                team_id=team_id_var.get(),
            )


async def _persist(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    payload: dict[str, Any],
    request_id: str,
    team_id: str | None,
) -> None:
    try:
        key_raw = payload.get("api_key_id")
        async with session_scope(session_factory) as session:
            await append_request_log(
                session,
                request_id=request_id,
                api_key_id=uuid.UUID(key_raw) if isinstance(key_raw, str) and key_raw else None,
                team_id=uuid.UUID(team_id) if team_id else None,
                method=str(payload["method"]),
                path=str(payload["path"]),
                status_code=int(payload["status_code"]),
                latency_ms=float(payload["latency_ms"]),
                model=payload.get("model"),
                provider=payload.get("provider"),
                route=payload.get("route"),
                cache=str(payload.get("cache") or "bypass"),
                stream=bool(payload.get("stream")),
                prompt_tokens=payload.get("prompt_tokens"),
                completion_tokens=payload.get("completion_tokens"),
                total_tokens=payload.get("total_tokens"),
                estimated_cost_usd=payload.get("estimated_cost_usd"),
                error=payload.get("error"),
            )
    except Exception as exc:
        structlog.get_logger("llmgate").warning(
            "request_log.failed",
            error=type(exc).__name__,
        )


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
