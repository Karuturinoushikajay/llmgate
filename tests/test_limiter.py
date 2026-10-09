from __future__ import annotations

import pytest

from llmgate.core.limiter import TokenBucketLimiter, format_reset
from llmgate.core.redis_client import RedisClient


@pytest.fixture
def limiter(redis_client: RedisClient) -> TokenBucketLimiter:
    return TokenBucketLimiter(redis_client)


def test_reset_header_matches_openai_style() -> None:
    assert format_reset(0) == "0s"
    assert format_reset(150) == "150ms"
    assert format_reset(2000) == "2s"
    assert format_reset(1500) == "1s500ms"
    assert format_reset(65_000) == "1m5s"


async def test_request_bucket_denies_without_going_negative(limiter: TokenBucketLimiter) -> None:
    now = 1_000_000
    kwargs = {
        "key_id": "requests",
        "requests_per_minute": 2,
        "tokens_per_minute": 0,
        "token_cost": 10,
    }
    first = await limiter.acquire(now_ms=now, **kwargs)
    second = await limiter.acquire(now_ms=now, **kwargs)
    third = await limiter.acquire(now_ms=now, **kwargs)
    assert first.allowed and second.allowed
    assert second.remaining_requests == 0
    assert third.allowed is False
    assert third.reason == 1
    error = third.as_error()
    assert error.status_code == 429
    assert error.retryable is False
    assert error.response_headers["retry-after"].isdigit()
    assert error.response_headers["x-ratelimit-limit-requests"] == "2"
    assert error.response_headers["x-ratelimit-remaining-requests"] == "0"
    assert int(error.response_headers["retry-after"]) >= 1


async def test_token_denial_does_not_consume_a_request(limiter: TokenBucketLimiter) -> None:
    now = 2_000_000
    allowed = await limiter.acquire(
        key_id="tokens",
        requests_per_minute=5,
        tokens_per_minute=100,
        token_cost=40,
        now_ms=now,
    )
    denied = await limiter.acquire(
        key_id="tokens",
        requests_per_minute=5,
        tokens_per_minute=100,
        token_cost=80,
        now_ms=now,
    )
    assert allowed.allowed is True
    assert allowed.remaining_requests == 4
    assert allowed.remaining_tokens == 60
    assert denied.allowed is False
    assert denied.reason == 2
    # The request bucket was not charged for a call we refused.
    assert denied.remaining_requests == 4
    assert denied.remaining_tokens == 60


async def test_refill_and_reconcile(limiter: TokenBucketLimiter) -> None:
    now = 3_000_000
    drained = await limiter.acquire(
        key_id="refill",
        requests_per_minute=1,
        tokens_per_minute=100,
        token_cost=40,
        now_ms=now,
    )
    blocked = await limiter.acquire(
        key_id="refill",
        requests_per_minute=1,
        tokens_per_minute=100,
        token_cost=1,
        now_ms=now,
    )
    assert drained.allowed is True
    assert blocked.allowed is False
    # One request per minute: a full window restores the single permit.
    restored = await limiter.acquire(
        key_id="refill",
        requests_per_minute=1,
        tokens_per_minute=100,
        token_cost=1,
        now_ms=now + 60_000,
    )
    assert restored.allowed is True

    remaining, _reset = await limiter.adjust(
        key_id="refill",
        tokens_per_minute=100,
        delta=-20,
        now_ms=now + 60_000,
    )
    # After the restored call, 99 tokens were left (100 - 1). Refunding 20 hits the cap.
    assert remaining == 100
    # Spending more than the bucket holds is remembered as an empty bucket.
    over, _reset = await limiter.adjust(
        key_id="refill",
        tokens_per_minute=100,
        delta=250,
        now_ms=now + 60_000,
    )
    assert over == 0
