"""Shared HTTP helpers for provider adapters."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from llmgate.core.errors import (
    InvalidRequestError,
    ProviderError,
    ProviderNotConfiguredError,
    RateLimitError,
)


def require_credentials(provider: str, api_key: str) -> None:
    if not api_key:
        raise ProviderNotConfiguredError(provider)


def request_timeout(timeout: httpx.Timeout | None) -> Any:
    """Use the per-call timeout, or the shared client's timeout when none was set.

    The return is ``Any`` because ``httpx.USE_CLIENT_DEFAULT`` is a private sentinel
    that the public stubs do not name, and ``post(..., timeout=)`` accepts it.
    """
    if timeout is None:
        return httpx.USE_CLIENT_DEFAULT
    return timeout


def parse_retry_after(headers: httpx.Headers | None) -> float | None:
    """Return a Retry-After delay in seconds, or None when the header is absent or invalid."""
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def raise_for_status(
    provider: str,
    status_code: int,
    body: bytes,
    headers: httpx.Headers | None = None,
) -> None:
    """Map an upstream HTTP error onto an OpenAI-style gateway error.

    Authentication failures stay 502. A 401 from the provider means the
    gateway's own credential is wrong; returning 401 would make clients
    rotate a gateway key that is fine.
    """
    if status_code in (401, 403):
        # Bad gateway credentials will not succeed on retry, and must not open the breaker.
        raise ProviderError(
            f"{provider} rejected the gateway credentials",
            status_code=502,
            retryable=False,
        )
    message = _error_message(provider, body)
    retry_after = parse_retry_after(headers)
    if status_code == 400:
        raise InvalidRequestError(message)
    if status_code == 404:
        raise InvalidRequestError(message, param="model", code="model_not_found")
    if status_code == 429:
        forwarded: dict[str, str] = {}
        if retry_after is not None:
            forwarded["retry-after"] = str(max(1, math.ceil(retry_after)))
        raise RateLimitError(message, retry_after=retry_after, response_headers=forwarded)
    if status_code in (408, 504):
        raise ProviderError(message, status_code=504, retryable=True)
    if status_code >= 500:
        raise ProviderError(message, status_code=502, retryable=True)
    raise ProviderError(message, status_code=502, retryable=False)


def _error_message(provider: str, body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return f"{provider} request failed"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:500]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message[:500]
        if isinstance(error, str) and error:
            return error[:500]
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message[:500]
    return text[:500]


async def iter_sse_json(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON objects from an SSE response. Ignores comments and ``[DONE]``."""
    async for line in response.aiter_lines():
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            if raw == "[DONE]":
                return
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def assign_if_set(payload: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        payload[key] = value
