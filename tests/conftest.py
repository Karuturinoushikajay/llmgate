"""Shared fixtures. Each test gets its own SQLite file and an in-memory Redis."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from redis.asyncio import Redis

from llmgate.config import Settings
from llmgate.core.redis_client import RedisClient
from llmgate.main import create_app
from tests.helpers import MASTER_KEY, ROOT


def build_settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": database_url,
        "redis_url": "redis://localhost:6379/0",
        "master_key": MASTER_KEY,
        "key_pepper": "test-pepper",
        "models_path": str(ROOT / "config" / "models.yaml"),
        "log_level": "INFO",
        "alembic_ini": str(ROOT / "alembic.ini"),
        "openai_api_key": "test-openai",
        "anthropic_api_key": "test-anthropic",
        "gemini_api_key": "test-gemini",
        "ollama_base_url": "http://ollama.test",
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture
async def redis_client() -> AsyncIterator[RedisClient]:
    """fakeredis (with lupa) locally. CI sets LLMGATE_TEST_REDIS_URL to real Redis."""
    url = os.environ.get("LLMGATE_TEST_REDIS_URL")
    if url:
        client: RedisClient = Redis.from_url(url, decode_responses=True)
        await client.flushdb()  # type: ignore[attr-defined]
    else:
        client = FakeAsyncRedis(decode_responses=True)
    try:
        yield client
    finally:
        if url:
            await client.flushdb()  # type: ignore[attr-defined]
        await client.aclose()


@pytest.fixture
async def client(tmp_path: Path, redis_client: RedisClient) -> AsyncIterator[httpx.AsyncClient]:
    async with _http_client(
        tmp_path,
        redis_client,
        retry_max_attempts=1,
        retry_base_delay_seconds=0,
    ) as http:
        yield http


@pytest.fixture
def make_client(
    tmp_path: Path,
    redis_client: RedisClient,
) -> Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]:
    def _make(**overrides: object) -> AbstractAsyncContextManager[httpx.AsyncClient]:
        settings = {"retry_max_attempts": 1, "retry_base_delay_seconds": 0}
        settings.update(overrides)
        return _http_client(tmp_path, redis_client, **settings)

    return _make


@asynccontextmanager
async def _http_client(
    tmp_path: Path,
    redis: RedisClient,
    **overrides: object,
) -> AsyncIterator[httpx.AsyncClient]:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'llmgate.db'}"
    app = create_app(build_settings(database_url, **overrides), redis_client=redis)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


@pytest.fixture
async def api_key(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/admin/keys",
        headers={"authorization": f"Bearer {MASTER_KEY}"},
        json={"name": "test"},
    )
    assert response.status_code == 201, response.text
    key = response.json()["key"]
    assert isinstance(key, str)
    return key
