"""Pipeline retries, fallback, and breaker behavior with in-process providers."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from llmgate.core.breaker import CircuitBreaker
from llmgate.core.errors import InvalidRequestError, ProviderError, RateLimitError
from llmgate.core.limiter import TokenBucketLimiter
from llmgate.core.pipeline import ChatPipeline, KeyLimits
from llmgate.core.registry import ModelEntry, ModelRegistry, ModelTarget
from llmgate.core.retry import RetryPolicy
from llmgate.core.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Usage,
    completion_chunk,
    completion_response,
)
from llmgate.providers.base import ChatProvider

_NOW = 10_000_000


class ScriptedProvider:
    def __init__(self, name: str, script: list[object]) -> None:
        self.name = name
        self.script = script
        self.calls = 0
        self.timeouts: list[httpx.Timeout | None] = []

    async def complete(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        *,
        upstream_timeout: httpx.Timeout | None = None,
    ) -> ChatCompletionResponse:
        self.calls += 1
        self.timeouts.append(upstream_timeout)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, ChatCompletionResponse):
            raise AssertionError("complete script must return a response")
        return item

    async def stream(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        *,
        upstream_timeout: httpx.Timeout | None = None,
    ) -> AsyncGenerator[ChatCompletionChunk, None]:
        self.calls += 1
        self.timeouts.append(upstream_timeout)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, list):
            raise AssertionError("stream script must return a list of chunks")
        for chunk in item:
            if isinstance(chunk, Exception):
                raise chunk
            if not isinstance(chunk, ChatCompletionChunk):
                raise AssertionError("stream chunks must be ChatCompletionChunk")
            yield chunk


def _ok(content: str = "Hello") -> ChatCompletionResponse:
    return completion_response(
        completion_id="chatcmpl-test",
        created=1,
        model="m",
        content=content,
        finish_reason="stop",
        usage=Usage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
    )


def _chunk(content: str = "Hi") -> ChatCompletionChunk:
    return completion_chunk(
        completion_id="chatcmpl-test",
        created=1,
        model="m",
        content=content,
        role="assistant",
    )


def _request() -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "Hello"}]}
    )


def _pipeline(
    providers: dict[str, ChatProvider],
    *,
    fallbacks: list[ModelTarget] | None = None,
    attempts: int = 1,
    threshold: int = 10,
    cooldown: float = 3600,
    rpm: int = 100,
    tpm: int = 10_000,
) -> tuple[ChatPipeline, list[float]]:
    registry = ModelRegistry(
        [
            ModelEntry(
                id="m",
                provider="openai",
                upstream_model="primary",
                fallbacks=fallbacks or [],
            )
        ],
        loaded_at=1,
    )
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    pipeline = ChatPipeline(
        registry,
        providers,
        retry_policy=RetryPolicy(
            max_attempts=attempts,
            base_delay_seconds=0.2,
            max_delay_seconds=8,
            retry_after_cap_seconds=30,
        ),
        breaker=CircuitBreaker(
            FakeAsyncRedis(decode_responses=True),
            failure_threshold=threshold,
            cooldown_seconds=cooldown,
            half_open_max=1,
        ),
        limiter=TokenBucketLimiter(FakeAsyncRedis(decode_responses=True)),
        output_token_reservation=8,
        sleeper=sleep,
        clock_ms=lambda: _NOW,
        random_unit=lambda: 0.0,
    )
    pipeline.default_rpm = rpm  # type: ignore[attr-defined]
    pipeline.default_tpm = tpm  # type: ignore[attr-defined]
    return pipeline, delays


def _limits(pipeline: ChatPipeline) -> KeyLimits:
    return KeyLimits(
        key_id="key",
        requests_per_minute=pipeline.default_rpm,  # type: ignore[attr-defined]
        tokens_per_minute=pipeline.default_tpm,  # type: ignore[attr-defined]
    )


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(5.0)


async def test_retries_then_succeeds_and_records_backoff() -> None:
    primary = ScriptedProvider("openai", [ProviderError("down", status_code=502), _ok()])
    pipeline, delays = _pipeline({"openai": primary}, attempts=3)
    served = await pipeline.complete(
        _request(), limits=_limits(pipeline), upstream_timeout=_timeout()
    )
    assert served.response.choices[0].message.content == "Hello"
    assert served.provider == "openai"
    assert primary.calls == 2
    assert delays == [pytest.approx(0.1)]
    assert primary.timeouts[0] is not None
    assert primary.timeouts[0].read == 5.0


async def test_client_error_is_not_retried_or_fallen_back() -> None:
    primary = ScriptedProvider("openai", [InvalidRequestError("bad json")])
    fallback = ScriptedProvider("anthropic", [_ok("fallback")])
    pipeline, delays = _pipeline(
        {"openai": primary, "anthropic": fallback},
        fallbacks=[ModelTarget(provider="anthropic", upstream_model="haiku")],
        attempts=3,
    )
    with pytest.raises(InvalidRequestError):
        await pipeline.complete(_request(), limits=_limits(pipeline), upstream_timeout=_timeout())
    assert primary.calls == 1
    assert fallback.calls == 0
    assert delays == []


async def test_exhausted_retries_fall_back_to_the_next_provider() -> None:
    primary = ScriptedProvider(
        "openai",
        [ProviderError("down", status_code=503), ProviderError("down", status_code=503)],
    )
    fallback = ScriptedProvider("anthropic", [_ok("from-fallback")])
    pipeline, _delays = _pipeline(
        {"openai": primary, "anthropic": fallback},
        fallbacks=[ModelTarget(provider="anthropic", upstream_model="haiku")],
        attempts=2,
    )
    served = await pipeline.complete(
        _request(), limits=_limits(pipeline), upstream_timeout=_timeout()
    )
    assert served.provider == "anthropic"
    assert served.upstream_model == "haiku"
    assert served.headers["x-llmgate-provider"] == "anthropic"
    assert served.headers["x-llmgate-model"] == "haiku"
    assert served.response.choices[0].message.content == "from-fallback"
    assert primary.calls == 2
    assert fallback.calls == 1


async def test_retry_after_is_the_wait_when_it_is_longer_than_backoff() -> None:
    primary = ScriptedProvider(
        "openai",
        [RateLimitError("slow", retry_after=2), _ok()],
    )
    pipeline, delays = _pipeline({"openai": primary}, attempts=2)
    await pipeline.complete(_request(), limits=_limits(pipeline), upstream_timeout=_timeout())
    assert delays == [2]


async def test_open_breaker_skips_the_provider() -> None:
    primary = ScriptedProvider(
        "openai", [ProviderError("down", status_code=502), _ok("should-not-run")]
    )
    fallback = ScriptedProvider("anthropic", [_ok("first"), _ok("second")])
    pipeline, _delays = _pipeline(
        {"openai": primary, "anthropic": fallback},
        fallbacks=[ModelTarget(provider="anthropic", upstream_model="haiku")],
        attempts=1,
        threshold=1,
        cooldown=3600,
    )
    limits = _limits(pipeline)
    first = await pipeline.complete(_request(), limits=limits, upstream_timeout=_timeout())
    second = await pipeline.complete(_request(), limits=limits, upstream_timeout=_timeout())
    assert first.provider == "anthropic"
    assert second.provider == "anthropic"
    assert second.response.choices[0].message.content == "second"
    assert primary.calls == 1
    assert fallback.calls == 2


async def test_stream_retries_only_before_the_first_chunk() -> None:
    chunk = _chunk("Hi")
    primary = ScriptedProvider(
        "openai",
        [
            ProviderError("down", status_code=502),
            [chunk, ProviderError("mid-stream", status_code=502)],
        ],
    )
    pipeline, delays = _pipeline({"openai": primary}, attempts=3)
    opened = await pipeline.open_stream(
        _request(), limits=_limits(pipeline), upstream_timeout=_timeout()
    )
    assert opened.first is not None
    assert opened.first.choices[0].delta.content == "Hi"
    assert primary.calls == 2
    assert delays == [pytest.approx(0.1)]
    with pytest.raises(ProviderError):
        await anext(opened.chunks)
    assert primary.calls == 2
    await opened.aclose()


async def test_missing_model_falls_back_but_rate_limit_does_not_open_the_breaker() -> None:
    primary = ScriptedProvider(
        "openai",
        [InvalidRequestError("gone", param="model", code="model_not_found")],
    )
    fallback = ScriptedProvider("anthropic", [_ok("other")])
    pipeline, _delays = _pipeline(
        {"openai": primary, "anthropic": fallback},
        fallbacks=[ModelTarget(provider="anthropic", upstream_model="haiku")],
        attempts=3,
        threshold=1,
    )
    served = await pipeline.complete(
        _request(), limits=_limits(pipeline), upstream_timeout=_timeout()
    )
    assert served.provider == "anthropic"
    assert primary.calls == 1
    # The miss was not a breaker failure, so the primary is still eligible.
    primary.script.append(_ok("back"))
    again = await pipeline.complete(
        _request(), limits=_limits(pipeline), upstream_timeout=_timeout()
    )
    assert again.provider == "openai"


async def test_gateway_rate_limit_is_429_before_the_provider() -> None:
    primary = ScriptedProvider("openai", [_ok(), _ok()])
    pipeline, _delays = _pipeline({"openai": primary}, rpm=1, tpm=0)
    limits = _limits(pipeline)
    await pipeline.complete(_request(), limits=limits, upstream_timeout=_timeout())
    with pytest.raises(RateLimitError) as exc:
        await pipeline.complete(_request(), limits=limits, upstream_timeout=_timeout())
    assert exc.value.status_code == 429
    assert exc.value.response_headers["x-ratelimit-limit-requests"] == "1"
    assert "retry-after" in exc.value.response_headers
    assert primary.calls == 1
