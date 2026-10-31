"""Shared fixtures. Each test gets its own SQLite file and an in-memory Redis."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from llmgate.config import Settings
from llmgate.main import create_app
from tests.helpers import MASTER_KEY, ROOT


async def _no_sleep(_seconds: float) -> None:
    return None


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
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'llmgate.db'}"
    app = create_app(
        build_settings(database_url), redis_client=FakeAsyncRedis(decode_responses=True)
    )
    async with app.router.lifespan_context(app):
        app.state.gateway.pipeline.sleep = _no_sleep
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
