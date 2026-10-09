from __future__ import annotations

import httpx
import pytest

from llmgate.core.errors import (
    InvalidRequestError,
    ProviderError,
    ProviderNotConfiguredError,
    RateLimitError,
)
from llmgate.core.retry import (
    RetryPolicy,
    backoff_delay,
    counts_toward_breaker,
    map_provider_exception,
    should_fallback,
    should_retry,
)


def _policy(**overrides: float) -> RetryPolicy:
    values: dict[str, float] = {
        "max_attempts": 3,
        "base_delay_seconds": 0.2,
        "max_delay_seconds": 8,
        "retry_after_cap_seconds": 30,
    }
    values.update(overrides)
    return RetryPolicy(
        max_attempts=int(values["max_attempts"]),
        base_delay_seconds=values["base_delay_seconds"],
        max_delay_seconds=values["max_delay_seconds"],
        retry_after_cap_seconds=values["retry_after_cap_seconds"],
    )


def test_equal_jitter_stays_inside_half_and_full_delay() -> None:
    policy = _policy()
    low = backoff_delay(attempt=1, policy=policy, retry_after=None, random_unit=0)
    high = backoff_delay(attempt=1, policy=policy, retry_after=None, random_unit=0.999)
    assert low == pytest.approx(0.1)
    assert high == pytest.approx(0.1999)
    # attempt 4 would be 0.2 * 8 = 1.6, still under the 8s cap. attempt 20 hits the cap.
    capped = backoff_delay(attempt=20, policy=policy, retry_after=None, random_unit=0)
    assert capped == pytest.approx(policy.max_delay_seconds / 2)


def test_retry_after_extends_the_wait_and_then_caps_it() -> None:
    policy = _policy()
    assert backoff_delay(attempt=1, policy=policy, retry_after=10, random_unit=0) == 10
    assert backoff_delay(attempt=1, policy=policy, retry_after=120, random_unit=1 - 1e-9) == 30


def test_short_retry_after_does_not_undercut_backoff() -> None:
    policy = _policy()
    delay = backoff_delay(attempt=1, policy=policy, retry_after=0.01, random_unit=0)
    assert delay == pytest.approx(0.1)


def test_client_errors_are_not_retried() -> None:
    policy = _policy(max_attempts=5)
    assert should_retry(InvalidRequestError("bad"), attempt=1, policy=policy) is False
    credentials = ProviderError("rejected", status_code=502, retryable=False)
    assert should_retry(credentials, attempt=1, policy=policy) is False
    assert should_fallback(InvalidRequestError("bad")) is False


def test_timeouts_connection_errors_429_and_5xx_are_retried() -> None:
    policy = _policy()
    assert should_retry(RateLimitError("slow"), attempt=1, policy=policy) is True
    assert should_retry(ProviderError("down", status_code=502), attempt=1, policy=policy) is True
    assert should_retry(ProviderError("hung", status_code=504), attempt=2, policy=policy) is True
    assert should_retry(ProviderError("down", status_code=502), attempt=3, policy=policy) is False
    timeout = map_provider_exception("openai", httpx.ReadTimeout("slow"))
    assert timeout.retryable is True
    assert timeout.status_code == 504
    offline = map_provider_exception("openai", httpx.ConnectError("no"))
    assert offline.retryable is True
    assert offline.status_code == 502


def test_breaker_ignores_rate_limits_and_client_errors() -> None:
    assert counts_toward_breaker(RateLimitError("slow")) is False
    assert counts_toward_breaker(ProviderNotConfiguredError("gemini")) is False
    assert counts_toward_breaker(ProviderError("down", status_code=503)) is True
    assert counts_toward_breaker(InvalidRequestError("bad")) is False
    missing = InvalidRequestError("missing", param="model", code="model_not_found")
    assert should_fallback(missing) is True
    assert should_fallback(ProviderNotConfiguredError("ollama")) is True
    assert counts_toward_breaker(missing) is False
