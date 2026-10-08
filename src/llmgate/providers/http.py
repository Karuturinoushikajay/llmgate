"""Shared HTTP helpers for provider adapters."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
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


def raise_for_status(provider: str, status_code: int, body: bytes) -> None:
    """Map an upstream HTTP error onto an OpenAI-style gateway error.

    Authentication failures stay 502. A 401 from the provider means the
    gateway's own credential is wrong; returning 401 would make clients
    rotate a gateway key that is fine.
    """
    if status_code in (401, 403):
        raise ProviderError(f"{provider} rejected the gateway credentials", status_code=502)
    message = _error_message(provider, body)
    if status_code == 400:
        raise InvalidRequestError(message)
    if status_code == 404:
        raise InvalidRequestError(message, param="model", code="model_not_found")
    if status_code == 429:
        raise RateLimitError(message)
    if status_code in (408, 504):
        raise ProviderError(message, status_code=504)
    raise ProviderError(message, status_code=502)


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
