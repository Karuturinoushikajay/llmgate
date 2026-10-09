"""Chaos demo for retries and fallback.

Runs entirely in process: a scripted provider fails with a fixed probability,
and the real ChatPipeline decides whether to retry or fall back. No network
and no provider credentials.

    python scripts/reliability_demo.py

The seed, trial count, and failure rates are constants below. Re-running the
script prints the same counts.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import httpx
from fakeredis import FakeAsyncRedis

from llmgate.core.breaker import CircuitBreaker
from llmgate.core.errors import GatewayError, ProviderError
from llmgate.core.limiter import TokenBucketLimiter
from llmgate.core.pipeline import ChatPipeline, KeyLimits
from llmgate.core.registry import ModelEntry, ModelRegistry, ModelTarget
from llmgate.core.retry import RetryPolicy
from llmgate.core.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Usage,
    completion_response,
)
from llmgate.logging import configure_logging
from llmgate.providers.base import ChatProvider

SEED = 7
TRIALS = 2000
PRIMARY_FAILURE_RATE = 0.50
FALLBACK_FAILURE_RATE = 0.10
ATTEMPTS = 3


class ChaosProvider:
    """Fails each call independently with ``failure_rate``. ``None`` never fails."""

    def __init__(self, name: str, rng: random.Random, failure_rate: float) -> None:
        self.name = name
        self.rng = rng
        self.failure_rate = failure_rate
        self.calls = 0

    async def complete(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        *,
        upstream_timeout: httpx.Timeout | None = None,
    ) -> ChatCompletionResponse:
        self.calls += 1
        if self.rng.random() < self.failure_rate:
            raise ProviderError("chaos failure", status_code=503, retryable=True)
        return completion_response(
            completion_id="chatcmpl-chaos",
            created=1,
            model=upstream_model,
            content="ok",
            finish_reason="stop",
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def stream(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        *,
        upstream_timeout: httpx.Timeout | None = None,
    ) -> AsyncGenerator[ChatCompletionChunk, None]:
        raise NotImplementedError
        yield  # pragma: no cover


@dataclass(frozen=True)
class TrialResult:
    label: str
    successes: int
    trials: int
    primary_calls: int
    fallback_calls: int

    @property
    def success_rate(self) -> float:
        return self.successes / self.trials


def _request() -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {"model": "chaos", "messages": [{"role": "user", "content": "ping"}]}
    )


def _pipeline(
    providers: dict[str, ChatProvider],
    *,
    fallbacks: list[ModelTarget],
    attempts: int,
    threshold: int,
    redis: FakeAsyncRedis,
) -> ChatPipeline:
    registry = ModelRegistry(
        [
            ModelEntry(
                id="chaos",
                provider="openai",
                upstream_model="primary",
                fallbacks=fallbacks,
            )
        ],
        loaded_at=1,
    )

    async def sleep(_seconds: float) -> None:
        return None

    return ChatPipeline(
        registry,
        providers,
        retry_policy=RetryPolicy(
            max_attempts=attempts,
            base_delay_seconds=0,
            max_delay_seconds=0,
            retry_after_cap_seconds=0,
        ),
        breaker=CircuitBreaker(
            redis,
            failure_threshold=threshold,
            cooldown_seconds=3600,
            half_open_max=1,
        ),
        limiter=TokenBucketLimiter(redis),
        output_token_reservation=1,
        sleeper=sleep,
        clock_ms=lambda: 1_000_000,
        random_unit=lambda: 0.0,
    )


async def _run_probability(label: str, *, attempts: int, with_fallback: bool) -> TrialResult:
    redis = FakeAsyncRedis(decode_responses=True)
    primary = ChaosProvider("openai", random.Random(SEED), PRIMARY_FAILURE_RATE)
    fallback = ChaosProvider("anthropic", random.Random(SEED + 1), FALLBACK_FAILURE_RATE)
    providers: dict[str, ChatProvider] = {"openai": primary}
    fallbacks: list[ModelTarget] = []
    if with_fallback:
        providers["anthropic"] = fallback
        fallbacks = [ModelTarget(provider="anthropic", upstream_model="haiku")]
    pipeline = _pipeline(
        providers,
        fallbacks=fallbacks,
        attempts=attempts,
        threshold=10_000,
        redis=redis,
    )
    limits = KeyLimits(key_id=label, requests_per_minute=0, tokens_per_minute=0)
    successes = 0
    for _ in range(TRIALS):
        try:
            await pipeline.complete(_request(), limits=limits, upstream_timeout=httpx.Timeout(1.0))
        except GatewayError:
            continue
        successes += 1
    await redis.aclose()
    fallback_calls = fallback.calls if with_fallback else 0
    return TrialResult(label, successes, TRIALS, primary.calls, fallback_calls)


async def _run_breaker() -> tuple[TrialResult, TrialResult]:
    """Primary always fails. After the threshold, an open breaker must not call it."""

    async def _one(with_fallback: bool) -> TrialResult:
        redis = FakeAsyncRedis(decode_responses=True)
        primary = ChaosProvider("openai", random.Random(0), 1.0)
        fallback = ChaosProvider("anthropic", random.Random(0), 0.0)
        providers: dict[str, ChatProvider] = {"openai": primary}
        fallbacks: list[ModelTarget] = []
        if with_fallback:
            providers["anthropic"] = fallback
            fallbacks = [ModelTarget(provider="anthropic", upstream_model="haiku")]
        pipeline = _pipeline(
            providers,
            fallbacks=fallbacks,
            attempts=1,
            threshold=3,
            redis=redis,
        )
        limits = KeyLimits(key_id="breaker", requests_per_minute=0, tokens_per_minute=0)
        successes = 0
        trials = 6
        for _ in range(trials):
            try:
                await pipeline.complete(
                    _request(), limits=limits, upstream_timeout=httpx.Timeout(1.0)
                )
            except GatewayError:
                continue
            successes += 1
        await redis.aclose()
        label = "breaker_with_fallback" if with_fallback else "breaker_without_fallback"
        return TrialResult(label, successes, trials, primary.calls, fallback.calls)

    return await _one(False), await _one(True)


def _line(result: TrialResult) -> str:
    percent = 100 * result.success_rate
    return (
        f"{result.label}: {result.successes}/{result.trials} "
        f"({percent:.2f}%) primary_calls={result.primary_calls} "
        f"fallback_calls={result.fallback_calls}"
    )


async def collect() -> list[TrialResult]:
    none = await _run_probability("no_retry_no_fallback", attempts=1, with_fallback=False)
    retries = await _run_probability("retries_only", attempts=ATTEMPTS, with_fallback=False)
    both = await _run_probability("retries_and_fallback", attempts=ATTEMPTS, with_fallback=True)
    closed, opened = await _run_breaker()
    return [none, retries, both, closed, opened]


def render(results: list[TrialResult]) -> str:
    header = (
        f"seed={SEED} trials={TRIALS} primary_failure_rate={PRIMARY_FAILURE_RATE} "
        f"fallback_failure_rate={FALLBACK_FAILURE_RATE} attempts={ATTEMPTS}"
    )
    return "\n".join([header, *[_line(result) for result in results]])


def main() -> str:
    configure_logging("ERROR")
    text = render(asyncio.run(collect()))
    print(text)
    return text


if __name__ == "__main__":
    main()
