"""Retries, fallback, circuit breaker, and the per-key token bucket."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import structlog
from fakeredis import FakeAsyncRedis

from llmgate.core.breaker import CircuitBreaker
from llmgate.core.errors import InvalidRequestError, ProviderError, RateLimitError
from llmgate.core.pipeline import ChatPipeline
from llmgate.core.ratelimit import TokenBucket
from llmgate.core.registry import ModelRegistry
from llmgate.core.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Usage,
    completion_chunk,
    completion_response,
)
from llmgate.main import create_app
from tests.conftest import _no_sleep, build_settings
from tests.helpers import MASTER_KEY, ROOT, auth_header, chat_body, parse_sse


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


class Script:
    def __init__(self, events: list[object]) -> None:
        self.events = list(events)
        self.calls = 0
        self.upstreams: list[str] = []

    async def complete(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
    ) -> ChatCompletionResponse:
        self.calls += 1
        self.upstreams.append(upstream_model)
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        assert isinstance(event, ChatCompletionResponse)
        return event

    async def stream(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
    ) -> AsyncIterator[ChatCompletionChunk]:
        self.calls += 1
        self.upstreams.append(upstream_model)
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        assert isinstance(event, ChatCompletionChunk)
        yield event


class MidStream:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
    ) -> ChatCompletionResponse:
        raise AssertionError(upstream_model)

    async def stream(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
    ) -> AsyncIterator[ChatCompletionChunk]:
        self.calls += 1
        yield _chunk("Yo")
        raise ProviderError("dropped", status_code=502)


class DownRedis:
    async def ping(self, **kwargs: object) -> bool:
        raise ConnectionError("down")

    async def aclose(self, *args: object, **kwargs: object) -> None:
        return None

    async def get(self, name: str) -> None:
        raise ConnectionError("down")

    async def set(self, name: str, value: str, **kwargs: object) -> None:
        raise ConnectionError("down")

    async def delete(self, *names: str) -> int:
        raise ConnectionError("down")

    async def incr(self, name: str) -> int:
        raise ConnectionError("down")


def _response(text: str = "Hello") -> ChatCompletionResponse:
    return completion_response(
        completion_id="chatcmpl-test",
        created=1,
        model="gpt-4o",
        content=text,
        finish_reason="stop",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def _chunk(text: str) -> ChatCompletionChunk:
    return completion_chunk(
        completion_id="chatcmpl-test",
        created=1,
        model="gpt-4o",
        content=text,
        role="assistant",
    )


def _request(model: str, **extra: object) -> ChatCompletionRequest:
    body: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
    }
    body.update(extra)
    return ChatCompletionRequest.model_validate(body)


def _pipeline(
    providers: dict[str, object],
    *,
    attempts: int = 3,
    threshold: int = 5,
    recovery: float = 30,
    clock: Clock | None = None,
    sleep: object | None = None,
) -> ChatPipeline:
    redis = FakeAsyncRedis(decode_responses=True)
    breaker = CircuitBreaker(
        redis,
        failure_threshold=threshold,
        recovery_seconds=recovery,
        now=clock,
    )
    registry = ModelRegistry.load(ROOT / "config" / "models.yaml", loaded_at=1)
    settings = build_settings("sqlite+aiosqlite://", retry_max_attempts=attempts)
    return ChatPipeline(
        registry,
        providers,  # type: ignore[arg-type]
        settings,
        breaker,
        sleep=sleep,  # type: ignore[arg-type]
    )


async def _issue(client: httpx.AsyncClient, name: str) -> str:
    response = await client.post(
        "/admin/keys",
        headers={"authorization": f"Bearer {MASTER_KEY}"},
        json={"name": name},
    )
    assert response.status_code == 201, response.text
    key = response.json()["key"]
    assert isinstance(key, str)
    return key


@pytest.fixture
async def started(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'llmgate.db'}"
    app = create_app(
        build_settings(database_url),
        redis_client=FakeAsyncRedis(decode_responses=True),
    )
    async with app.router.lifespan_context(app):
        app.state.gateway.pipeline.sleep = _no_sleep
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http, app


async def test_bucket_is_per_key_and_refills() -> None:
    clock = Clock()
    bucket = TokenBucket(
        FakeAsyncRedis(decode_responses=True),
        burst=1,
        refill_per_second=1,
        now=clock,
    )
    await bucket.consume("alpha")
    await bucket.consume("beta")
    with pytest.raises(RateLimitError) as exc:
        await bucket.consume("alpha")
    assert exc.value.retry_after == 1
    clock.t += 1
    await bucket.consume("alpha")


async def test_retries_transient_failures_and_not_client_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("llmgate.core.pipeline.random.uniform", lambda _start, end: end)
    slept: list[float] = []

    async def spy(seconds: float) -> None:
        slept.append(seconds)

    flaky = Script(
        [
            ProviderError("down", status_code=502),
            ProviderError("down", status_code=502),
            _response(),
        ]
    )
    pipeline = _pipeline({"openai": flaky}, sleep=spy)
    result = await pipeline.complete(_request("gpt-4o"))
    assert result.choices[0].message.content == "Hello"
    assert flaky.calls == 3
    assert slept == [0.2, 0.4]

    rejected = Script([InvalidRequestError("bad messages", param="messages")])
    pipeline = _pipeline({"openai": rejected}, sleep=spy)
    slept.clear()
    with pytest.raises(InvalidRequestError):
        await pipeline.complete(_request("gpt-4o"))
    assert rejected.calls == 1
    assert slept == []


async def test_fallback_uses_the_next_model_except_for_tools() -> None:
    openai = Script([ProviderError("down", status_code=502)] * 3)
    anthropic = Script([_response("from claude")])
    gemini = Script([_response("from gemini")])
    pipeline = _pipeline({"openai": openai, "anthropic": anthropic, "gemini": gemini})
    result = await pipeline.complete(_request("gpt-4o-mini"))
    assert result.choices[0].message.content == "from claude"
    assert openai.calls == 3
    assert anthropic.calls == 1
    assert anthropic.upstreams == ["claude-3-5-haiku-20241022"]
    assert gemini.calls == 0

    openai_tools = Script([ProviderError("down", status_code=502)] * 3)
    anthropic_tools = Script([_response()])
    pipeline = _pipeline({"openai": openai_tools, "anthropic": anthropic_tools})
    with pytest.raises(ProviderError) as exc:
        await pipeline.complete(
            _request("gpt-4o-mini", tools=[{"type": "function", "function": {"name": "ping"}}])
        )
    assert exc.value.status_code == 502
    assert openai_tools.calls == 3
    assert anthropic_tools.calls == 0


async def test_prefixed_model_has_no_fallback() -> None:
    openai = Script([ProviderError("down", status_code=502)] * 3)
    anthropic = Script([_response()])
    pipeline = _pipeline({"openai": openai, "anthropic": anthropic})
    with pytest.raises(ProviderError):
        await pipeline.complete(_request("openai/gpt-4o"))
    assert anthropic.calls == 0


async def test_breaker_opens_then_allows_one_probe() -> None:
    clock = Clock()
    openai = Script([ProviderError("down", status_code=502)] * 6)
    pipeline = _pipeline({"openai": openai}, attempts=1, clock=clock)
    for _ in range(5):
        with pytest.raises(ProviderError) as exc:
            await pipeline.complete(_request("gpt-4o"))
        assert exc.value.status_code == 502
    assert openai.calls == 5

    with pytest.raises(ProviderError) as skipped:
        await pipeline.complete(_request("gpt-4o"))
    assert skipped.value.status_code == 503
    assert openai.calls == 5

    clock.t += 30
    with pytest.raises(ProviderError) as probe:
        await pipeline.complete(_request("gpt-4o"))
    assert probe.value.status_code == 502
    assert openai.calls == 6

    with pytest.raises(ProviderError) as closed_again:
        await pipeline.complete(_request("gpt-4o"))
    assert closed_again.value.status_code == 503
    assert openai.calls == 6


async def test_breaker_fails_open_when_redis_errors() -> None:
    breaker = CircuitBreaker(DownRedis(), failure_threshold=1, recovery_seconds=30)
    assert await breaker.allow("openai") is True


async def test_stream_retries_before_the_first_chunk() -> None:
    slept: list[float] = []

    async def spy(seconds: float) -> None:
        slept.append(seconds)

    openai = Script([ProviderError("down", status_code=502), _chunk("Hi")])
    pipeline = _pipeline({"openai": openai}, sleep=spy)
    chunks = [chunk async for chunk in pipeline.stream(_request("gpt-4o"))]
    assert openai.calls == 2
    assert len(slept) == 1
    assert chunks[0].choices[0].delta.content == "Hi"


async def test_stream_error_after_first_chunk_is_not_retried(
    started: tuple[httpx.AsyncClient, object],
) -> None:
    client, app = started
    provider = MidStream()
    app.state.gateway.providers["openai"] = provider  # type: ignore[attr-defined]
    key = await _issue(client, "stream")
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_header(key),
        json=chat_body("gpt-4o", stream=True),
    )
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    assert events[-1] == "[DONE]"
    assert any(
        isinstance(event, dict) and event.get("error", {}).get("message") == "dropped"
        for event in events
    )
    assert provider.calls == 1


async def test_fallback_is_recorded_on_the_access_line(
    started: tuple[httpx.AsyncClient, object],
) -> None:
    client, app = started
    app.state.gateway.providers["openai"] = Script(  # type: ignore[attr-defined]
        [ProviderError("down", status_code=502)] * 3
    )
    app.state.gateway.providers["anthropic"] = Script([_response("from claude")])  # type: ignore[attr-defined]
    key = await _issue(client, "fallback")
    with structlog.testing.capture_logs() as logs:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o-mini"),
        )
    assert response.status_code == 200, response.text
    completed = [
        event
        for event in logs
        if event.get("event") == "request.completed" and event.get("path") == "/v1/chat/completions"
    ]
    assert completed[-1]["provider"] == "anthropic"
    assert completed[-1]["model"] == "claude-3-5-haiku"


async def test_rate_limit_response_sets_retry_after(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'llmgate.db'}"
    app = create_app(
        build_settings(database_url, rate_limit_burst=1, rate_limit_refill_per_second=1),
        redis_client=FakeAsyncRedis(decode_responses=True),
    )
    async with app.router.lifespan_context(app):
        app.state.gateway.pipeline.sleep = _no_sleep
        app.state.gateway.providers["openai"] = Script([_response(), _response()])
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first_key = await _issue(client, "first")
            second_key = await _issue(client, "second")
            ok = await client.post(
                "/v1/chat/completions",
                headers=auth_header(first_key),
                json=chat_body("gpt-4o"),
            )
            limited = await client.post(
                "/v1/chat/completions",
                headers=auth_header(first_key),
                json=chat_body("gpt-4o"),
            )
            other = await client.post(
                "/v1/chat/completions",
                headers=auth_header(second_key),
                json=chat_body("gpt-4o"),
            )
    assert ok.status_code == 200, ok.text
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "1"
    assert limited.json()["error"]["type"] == "rate_limit_error"
    assert other.status_code == 200, other.text


async def test_redis_down_does_not_call_a_provider(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'llmgate.db'}"
    app = create_app(build_settings(database_url), redis_client=DownRedis())
    provider = Script([_response()])
    async with app.router.lifespan_context(app):
        app.state.gateway.pipeline.sleep = _no_sleep
        app.state.gateway.providers["openai"] = provider
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            key = await _issue(client, "down")
            response = await client.post(
                "/v1/chat/completions",
                headers=auth_header(key),
                json=chat_body("gpt-4o"),
            )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "rate_limiter_unavailable"
    assert provider.calls == 0
