"""Routing, PII masking, the injection denylist, and team budgets."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fakeredis import FakeAsyncRedis

from llmgate.core.cache import MemoryCache
from llmgate.core.errors import ProviderError
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
from tests.helpers import MASTER_KEY, auth_header, chat_body


class Echo:
    def __init__(self, *, total: int = 2, fail_stream: bool = False) -> None:
        self.calls = 0
        self.upstreams: list[str] = []
        self.seen: list[object] = []
        self.total = total
        self.fail_stream = fail_stream

    async def complete(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
    ) -> ChatCompletionResponse:
        self.calls += 1
        self.upstreams.append(upstream_model)
        self.seen.append(request.messages[-1].content)
        return completion_response(
            completion_id="chatcmpl-test",
            created=1,
            model=request.model,
            content="ok",
            finish_reason="stop",
            usage=Usage(total_tokens=self.total),
        )

    async def stream(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
    ) -> AsyncIterator[ChatCompletionChunk]:
        self.calls += 1
        self.upstreams.append(upstream_model)
        yield completion_chunk(
            completion_id="chatcmpl-test",
            created=1,
            model=request.model,
            content="ok",
            role="assistant",
            usage=Usage(total_tokens=self.total),
        )
        if self.fail_stream:
            raise ProviderError("dropped", status_code=502)


class FixedEmbedder:
    async def embed(self, text: str) -> list[float]:
        if "other" in text:
            return [0.0, 1.0]
        return [1.0, 0.0]


class BudgetDown:
    """Rate limiter works. The budget key does not."""

    def __init__(self) -> None:
        self.inner = FakeAsyncRedis(decode_responses=True)

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

    async def get(self, name: str) -> object:
        if str(name).startswith("llmgate:budget:"):
            raise ConnectionError("down")
        return await self.inner.get(name)


async def _client(
    tmp_path: Path,
    provider: Echo,
    *,
    redis: object | None = None,
    cache: MemoryCache | None = None,
    embedder: FixedEmbedder | None = None,
    database: str = "llmgate.db",
) -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    database_url = f"sqlite+aiosqlite:///{tmp_path / database}"
    app = create_app(
        build_settings(database_url),
        redis_client=redis or FakeAsyncRedis(decode_responses=True),  # type: ignore[arg-type]
        semantic_cache=cache,
        embedder=embedder,
    )
    async with app.router.lifespan_context(app):
        app.state.gateway.pipeline.sleep = _no_sleep
        app.state.gateway.providers["openai"] = provider
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, app


async def _key(client: httpx.AsyncClient, *, team_id: str | None = None) -> str:
    body: dict[str, object] = {"name": "guard"}
    if team_id is not None:
        body["team_id"] = team_id
    response = await client.post(
        "/admin/keys",
        headers={"authorization": f"Bearer {MASTER_KEY}"},
        json=body,
    )
    assert response.status_code == 201, response.text
    key = response.json()["key"]
    assert isinstance(key, str)
    return key


async def _team(client: httpx.AsyncClient, budget: int | None) -> str:
    response = await client.post(
        "/admin/teams",
        headers={"authorization": f"Bearer {MASTER_KEY}"},
        json={"name": "acme", "monthly_token_budget": budget},
    )
    assert response.status_code == 201, response.text
    team_id = response.json()["id"]
    assert isinstance(team_id, str)
    return team_id


def _month_key(team_id: str) -> str:
    return f"llmgate:budget:{team_id}:{datetime.now(UTC):%Y-%m}"


async def test_auto_routes_by_complexity(tmp_path: Path) -> None:
    provider = Echo()
    async for client, _app in _client(tmp_path, provider):
        key = await _key(client)
        short = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("auto", messages=[{"role": "user", "content": "Hi"}]),
        )
        fenced = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body(
                "auto",
                messages=[{"role": "user", "content": "```\nprint(1)\n```"}],
            ),
        )
        long = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("auto", messages=[{"role": "user", "content": "x" * 300}]),
        )
        named = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o"),
        )
        prefix = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("openai/gpt-4o"),
        )
    assert short.status_code == 200, short.text
    assert short.json()["model"] == "auto"
    assert short.headers["x-llmgate-route"] == "gpt-4o-mini"
    assert fenced.headers["x-llmgate-route"] == "gpt-4o"
    assert long.headers["x-llmgate-route"] == "gpt-4o"
    assert named.headers["x-llmgate-route"] == "bypass"
    assert prefix.headers["x-llmgate-route"] == "bypass"
    assert provider.upstreams == [
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-4o",
        "gpt-4o",
        "gpt-4o",
    ]


async def test_pii_is_masked_before_the_provider(tmp_path: Path) -> None:
    provider = Echo()
    async for client, _app in _client(tmp_path, provider):
        key = await _key(client)
        secret = "mail a@b.co and ssn 123-45-6789"
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o", messages=[{"role": "user", "content": secret}]),
        )
    assert response.status_code == 200, response.text
    sent = provider.seen[0]
    assert isinstance(sent, str)
    assert "a@b.co" not in sent
    assert "123-45-6789" not in sent
    assert "[EMAIL]" in sent
    assert "[SSN]" in sent


async def test_injection_is_rejected_without_calling_the_provider(tmp_path: Path) -> None:
    provider = Echo()
    phrase = "ignore previous instructions"
    async for client, _app in _client(tmp_path, provider):
        key = await _key(client)
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o", messages=[{"role": "user", "content": phrase}]),
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "content_policy"
    assert phrase not in response.text
    assert provider.calls == 0


async def test_budget_blocks_the_next_call_and_a_cache_hit_is_free(tmp_path: Path) -> None:
    provider = Echo(total=5)
    async for client, app in _client(
        tmp_path,
        provider,
        cache=MemoryCache(similarity=0.92, ttl_seconds=600),
        embedder=FixedEmbedder(),
    ):
        team_id = await _team(client, 5)
        key = await _key(client, team_id=team_id)
        first = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o", temperature=0),
        )
        hit = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o", temperature=0),
        )
        stored = await app.state.gateway.redis.get(_month_key(team_id))  # type: ignore[attr-defined]
        blocked = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body(
                "gpt-4o",
                temperature=0,
                messages=[{"role": "user", "content": "other topic"}],
            ),
        )
    assert first.status_code == 200, first.text
    assert hit.status_code == 200, hit.text
    assert hit.headers["x-llmgate-cache"] == "hit"
    assert int(stored) == 5
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "budget_exceeded"
    assert provider.calls == 1


async def test_key_without_a_team_is_unlimited(tmp_path: Path) -> None:
    provider = Echo(total=100)
    async for client, _app in _client(tmp_path, provider):
        key = await _key(client)
        first = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o"),
        )
        second = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o"),
        )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert provider.calls == 2


async def test_budget_redis_failure_does_not_call_the_provider(tmp_path: Path) -> None:
    provider = Echo()
    async for client, _app in _client(tmp_path, provider, redis=BudgetDown()):
        team_id = await _team(client, 10)
        key = await _key(client, team_id=team_id)
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o"),
        )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "budget_unavailable"
    assert provider.calls == 0


async def test_finished_stream_is_charged_and_a_failed_stream_is_not(tmp_path: Path) -> None:
    finished = Echo(total=4)
    async for client, app in _client(tmp_path, finished, database="ok.db"):
        team_id = await _team(client, 10)
        key = await _key(client, team_id=team_id)
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o", stream=True),
        )
        stored = await app.state.gateway.redis.get(_month_key(team_id))  # type: ignore[attr-defined]
    assert response.status_code == 200, response.text
    assert "data: [DONE]" in response.text
    assert int(stored) == 4

    failed = Echo(total=50, fail_stream=True)
    async for client, _app in _client(tmp_path, failed, database="bad.db"):
        team_id = await _team(client, 1)
        key = await _key(client, team_id=team_id)
        first = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o", stream=True),
        )
        second = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o", stream=True),
        )
    assert first.status_code == 200
    assert "dropped" in first.text
    assert second.status_code == 200
    assert failed.calls == 2
