"""Semantic cache hits, misses, and bypasses. No real embedding API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from llmgate.core.cache import MemoryCache, cosine_similarity
from llmgate.core.schemas import ChatCompletionResponse
from llmgate.main import create_app
from tests.conftest import _no_sleep, build_settings
from tests.helpers import MASTER_KEY, auth_header, chat_body, parse_sse
from tests.test_reliability import MidStream, Script, _chunk, _response


class MappedEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        if "unrelated" in text:
            return [0.0, 1.0]
        if "almost" in text:
            return [0.96, 0.28]
        return [1.0, 0.0]


class BoomEmbedder:
    async def embed(self, text: str) -> list[float]:
        raise RuntimeError(text)


class BoomCache:
    async def lookup(self, *, model: str, vector: list[float]) -> None:
        raise RuntimeError(model)

    async def store(
        self,
        *,
        model: str,
        vector: list[float],
        response: ChatCompletionResponse,
    ) -> None:
        raise RuntimeError(response.id)


def test_cosine_threshold_and_model_isolation() -> None:
    assert cosine_similarity([1.0, 0.0], [0.96, 0.28]) >= 0.92
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0


async def test_memory_cache_hits_a_near_duplicate_for_the_same_model() -> None:
    cache = MemoryCache(similarity=0.92, ttl_seconds=60)
    stored = _response("cached")
    await cache.store(model="gpt-4o", vector=[1.0, 0.0], response=stored)
    hit = await cache.lookup(model="gpt-4o", vector=[0.96, 0.28])
    miss = await cache.lookup(model="gpt-4o", vector=[0.0, 1.0])
    other = await cache.lookup(model="gpt-4o-mini", vector=[1.0, 0.0])
    assert hit is not None
    assert hit.response.choices[0].message.content == "cached"
    assert hit.score >= 0.92
    assert miss is None
    assert other is None


@pytest.fixture
async def cached_app(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, object, MappedEmbedder]]:
    embedder = MappedEmbedder()
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'llmgate.db'}"
    app = create_app(
        build_settings(database_url),
        redis_client=FakeAsyncRedis(decode_responses=True),
        semantic_cache=MemoryCache(similarity=0.92, ttl_seconds=600),
        embedder=embedder,
    )
    async with app.router.lifespan_context(app):
        app.state.gateway.pipeline.sleep = _no_sleep
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, app, embedder


async def _issue(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/admin/keys",
        headers={"authorization": f"Bearer {MASTER_KEY}"},
        json={"name": "cache"},
    )
    assert response.status_code == 201, response.text
    key = response.json()["key"]
    assert isinstance(key, str)
    return key


async def test_second_identical_call_is_a_cache_hit(
    cached_app: tuple[httpx.AsyncClient, object, MappedEmbedder],
) -> None:
    client, app, embedder = cached_app
    provider = Script([_response("Hello")])
    app.state.gateway.providers["openai"] = provider  # type: ignore[attr-defined]
    key = await _issue(client)
    body = chat_body("gpt-4o", temperature=0)
    first = await client.post("/v1/chat/completions", headers=auth_header(key), json=body)
    second = await client.post("/v1/chat/completions", headers=auth_header(key), json=body)
    assert first.status_code == 200, first.text
    assert first.headers["x-llmgate-cache"] == "miss"
    assert second.status_code == 200, second.text
    assert second.headers["x-llmgate-cache"] == "hit"
    assert second.json()["choices"][0]["message"]["content"] == "Hello"
    assert provider.calls == 1
    assert embedder.calls == 2


async def test_near_duplicate_message_hits(
    cached_app: tuple[httpx.AsyncClient, object, MappedEmbedder],
) -> None:
    client, app, _embedder = cached_app
    provider = Script([_response("Hello")])
    app.state.gateway.providers["openai"] = provider  # type: ignore[attr-defined]
    key = await _issue(client)
    first = await client.post(
        "/v1/chat/completions",
        headers=auth_header(key),
        json=chat_body("gpt-4o", temperature=0, messages=[{"role": "user", "content": "Hello"}]),
    )
    second = await client.post(
        "/v1/chat/completions",
        headers=auth_header(key),
        json=chat_body(
            "gpt-4o",
            temperature=0,
            messages=[{"role": "user", "content": "almost the same"}],
        ),
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.headers["x-llmgate-cache"] == "hit"
    assert second.json()["choices"][0]["message"]["content"] == "Hello"
    assert provider.calls == 1


async def test_temperature_and_tools_bypass_the_cache(
    cached_app: tuple[httpx.AsyncClient, object, MappedEmbedder],
) -> None:
    client, app, embedder = cached_app
    provider = Script([_response(), _response(), _response()])
    app.state.gateway.providers["openai"] = provider  # type: ignore[attr-defined]
    key = await _issue(client)
    warm = await client.post(
        "/v1/chat/completions",
        headers=auth_header(key),
        json=chat_body("gpt-4o", temperature=0.7),
    )
    omitted = await client.post(
        "/v1/chat/completions",
        headers=auth_header(key),
        json=chat_body("gpt-4o"),
    )
    tools = await client.post(
        "/v1/chat/completions",
        headers=auth_header(key),
        json=chat_body(
            "gpt-4o",
            temperature=0,
            tools=[{"type": "function", "function": {"name": "ping"}}],
        ),
    )
    assert warm.status_code == 200, warm.text
    assert warm.headers["x-llmgate-cache"] == "bypass"
    assert omitted.status_code == 200, omitted.text
    assert omitted.headers["x-llmgate-cache"] == "bypass"
    assert tools.status_code == 200, tools.text
    assert tools.headers["x-llmgate-cache"] == "bypass"
    assert provider.calls == 3
    assert embedder.calls == 0


async def test_stream_hit_replays_sse_and_an_error_is_not_stored(
    cached_app: tuple[httpx.AsyncClient, object, MappedEmbedder],
) -> None:
    client, app, _embedder = cached_app
    provider = Script([_chunk("Hi")])
    app.state.gateway.providers["openai"] = provider  # type: ignore[attr-defined]
    key = await _issue(client)
    body = chat_body("gpt-4o", stream=True, temperature=0)
    first = await client.post("/v1/chat/completions", headers=auth_header(key), json=body)
    second = await client.post("/v1/chat/completions", headers=auth_header(key), json=body)
    assert first.headers["x-llmgate-cache"] == "miss"
    assert second.headers["x-llmgate-cache"] == "hit"
    events = parse_sse(second.text)
    assert events[-1] == "[DONE]"
    assert any(
        isinstance(event, dict) and event["choices"][0]["delta"].get("content") == "Hi"
        for event in events
        if isinstance(event, dict) and event.get("choices")
    )
    assert provider.calls == 1


async def test_failed_stream_is_not_cached(
    cached_app: tuple[httpx.AsyncClient, object, MappedEmbedder],
) -> None:
    client, app, _embedder = cached_app
    provider = MidStream()
    app.state.gateway.providers["openai"] = provider  # type: ignore[attr-defined]
    key = await _issue(client)
    body = chat_body("gpt-4o", stream=True, temperature=0)
    first = await client.post("/v1/chat/completions", headers=auth_header(key), json=body)
    second = await client.post("/v1/chat/completions", headers=auth_header(key), json=body)
    assert first.status_code == 200
    assert "dropped" in first.text
    assert second.headers["x-llmgate-cache"] == "miss"
    assert provider.calls == 2


async def test_embedding_or_cache_failure_still_calls_the_provider(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'llmgate.db'}"
    app = create_app(
        build_settings(database_url),
        redis_client=FakeAsyncRedis(decode_responses=True),
        semantic_cache=BoomCache(),
        embedder=BoomEmbedder(),
    )
    provider = Script([_response("still here"), _response("still here")])
    async with app.router.lifespan_context(app):
        app.state.gateway.pipeline.sleep = _no_sleep
        app.state.gateway.providers["openai"] = provider
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            key = await _issue(client)
            failed_embed = await client.post(
                "/v1/chat/completions",
                headers=auth_header(key),
                json=chat_body("gpt-4o", temperature=0),
            )
    assert failed_embed.status_code == 200, failed_embed.text
    assert failed_embed.headers["x-llmgate-cache"] == "bypass"
    assert failed_embed.json()["choices"][0]["message"]["content"] == "still here"
    assert provider.calls == 1

    app = create_app(
        build_settings(database_url),
        redis_client=FakeAsyncRedis(decode_responses=True),
        semantic_cache=BoomCache(),
        embedder=MappedEmbedder(),
    )
    provider = Script([_response("from provider")])
    async with app.router.lifespan_context(app):
        app.state.gateway.providers["openai"] = provider
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            key = await _issue(client)
            failed_cache = await client.post(
                "/v1/chat/completions",
                headers=auth_header(key),
                json=chat_body("gpt-4o", temperature=0),
            )
    assert failed_cache.status_code == 200, failed_cache.text
    assert failed_cache.headers["x-llmgate-cache"] == "bypass"
    assert provider.calls == 1
