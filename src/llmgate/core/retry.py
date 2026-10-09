"""Exponential backoff for retryable upstream failures.

Non-retryable 4xx stop immediately. ``Retry-After`` can extend a wait, but it
cannot stretch past ``retry_after_cap_seconds`` — a hostile or buggy upstream
must not pin a gateway worker for minutes.
"""

from __future__ import annotations

import httpx

from llmgate.core.errors import GatewayError, ProviderNotConfiguredError, RateLimitError


class RetryPolicy:
    def __init__(
        self,
        *,
        max_attempts: int,
        base_delay_seconds: float,
        max_delay_seconds: float,
        retry_after_cap_seconds: float,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if base_delay_seconds < 0 or max_delay_seconds < 0 or retry_after_cap_seconds < 0:
            raise ValueError("retry delays must be non-negative")
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.retry_after_cap_seconds = retry_after_cap_seconds


def backoff_delay(
    *,
    attempt: int,
    policy: RetryPolicy,
    retry_after: float | None,
    random_unit: float,
) -> float:
    """Seconds to sleep after attempt ``attempt`` (1 = first failure).

    Equal jitter keeps half of the exponential delay and randomizes the rest,
    so synchronized clients do not retry on the same millisecond. ``random_unit``
    is in ``[0, 1)``.
    """
    if not 0.0 <= random_unit < 1.0:
        raise ValueError("random_unit must be in [0, 1)")
    exponent = attempt - 1 if attempt > 1 else 0
    exponential = policy.base_delay_seconds
    for _ in range(exponent):
        exponential *= 2
    if exponential > policy.max_delay_seconds:
        exponential = policy.max_delay_seconds
    delay = exponential * 0.5 + exponential * 0.5 * random_unit
    if retry_after is None:
        return delay
    # Honor Retry-After even when it is longer than our own backoff, then cap it.
    extended = retry_after if retry_after > delay else delay
    if extended > policy.retry_after_cap_seconds:
        return policy.retry_after_cap_seconds
    return extended


def map_provider_exception(provider: str, exc: Exception) -> GatewayError:
    """Normalize an upstream failure into a gateway error the retry loop understands."""
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return GatewayError(
            f"Timed out waiting for {provider}",
            status_code=504,
            error_type="timeout",
            code="provider_timeout",
            retryable=True,
        )
    if isinstance(exc, httpx.HTTPError):
        return GatewayError(
            f"Failed to reach {provider}",
            status_code=502,
            error_type="server_error",
            code="provider_connection",
            retryable=True,
        )
    raise exc


def should_retry(exc: GatewayError, *, attempt: int, policy: RetryPolicy) -> bool:
    return exc.retryable and attempt < policy.max_attempts


def should_fallback(exc: GatewayError) -> bool:
    """True when a different provider might still serve this request."""
    if isinstance(exc, ProviderNotConfiguredError):
        return True
    if exc.code in {"circuit_open", "model_not_found"}:
        return True
    return exc.retryable


def counts_toward_breaker(exc: GatewayError) -> bool:
    """5xx and network failures trip the breaker. 429 means the provider is up."""
    if isinstance(exc, (RateLimitError, ProviderNotConfiguredError)):
        return False
    if exc.code in {"circuit_open", "model_not_found"}:
        return False
    return exc.retryable
