from __future__ import annotations

import pytest

from llmgate.core.breaker import CircuitBreaker
from llmgate.core.redis_client import RedisClient


@pytest.fixture
def breaker(redis_client: RedisClient) -> CircuitBreaker:
    return CircuitBreaker(
        redis_client,
        failure_threshold=2,
        cooldown_seconds=30,
        half_open_max=1,
    )


async def test_threshold_opens_and_cooldown_half_opens(breaker: CircuitBreaker) -> None:
    now = 1_000_000
    assert await breaker.allow("openai", now_ms=now) is True
    assert await breaker.record_failure("openai", now_ms=now) == "closed"
    assert await breaker.record_failure("openai", now_ms=now) == "open"
    assert await breaker.allow("openai", now_ms=now + 1_000) is False
    # Cooldown elapsed: one probe, and a second caller waits.
    assert await breaker.allow("openai", now_ms=now + 30_000) is True
    assert await breaker.allow("openai", now_ms=now + 30_000) is False


async def test_probe_success_closes_and_resets_the_failure_count(breaker: CircuitBreaker) -> None:
    now = 5_000_000
    await breaker.record_failure("anthropic", now_ms=now)
    await breaker.record_failure("anthropic", now_ms=now)
    assert await breaker.allow("anthropic", now_ms=now + 30_000) is True
    await breaker.record_success("anthropic", now_ms=now + 30_000)
    assert await breaker.allow("anthropic", now_ms=now + 30_000) is True
    # The previous streak is gone. One new failure does not reopen.
    assert await breaker.record_failure("anthropic", now_ms=now + 31_000) == "closed"
    assert await breaker.allow("anthropic", now_ms=now + 31_000) is True


async def test_probe_failure_reopens_for_a_full_cooldown(breaker: CircuitBreaker) -> None:
    now = 9_000_000
    await breaker.record_failure("gemini", now_ms=now)
    await breaker.record_failure("gemini", now_ms=now)
    assert await breaker.allow("gemini", now_ms=now + 30_000) is True
    assert await breaker.record_failure("gemini", now_ms=now + 30_000) == "open"
    assert await breaker.allow("gemini", now_ms=now + 30_000) is False
    assert await breaker.allow("gemini", now_ms=now + 60_000) is True


async def test_breakers_are_independent_per_provider(breaker: CircuitBreaker) -> None:
    now = 2_000
    await breaker.record_failure("openai", now_ms=now)
    await breaker.record_failure("openai", now_ms=now)
    assert await breaker.allow("openai", now_ms=now) is False
    assert await breaker.allow("ollama", now_ms=now) is True
