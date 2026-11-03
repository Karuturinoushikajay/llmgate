"""Request logs, audit trail, metrics, pricing, and RBAC viewers."""

from __future__ import annotations

from pathlib import Path

import httpx
from fakeredis import FakeAsyncRedis

from llmgate.core.pricing import PricingTable
from llmgate.main import create_app
from tests.conftest import _no_sleep, build_settings
from tests.helpers import MASTER_KEY, ROOT, auth_header, chat_body
from tests.test_reliability import Script, _response


def test_pricing_estimates_and_cache_hits_are_free() -> None:
    table = PricingTable.load(ROOT / "config" / "pricing.yaml")
    cost = table.estimate(
        "gpt-4o-mini",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        cache="miss",
    )
    assert cost == 0.15
    assert (
        table.estimate(
            "gpt-4o-mini",
            prompt_tokens=100,
            completion_tokens=100,
            cache="hit",
        )
        == 0.0
    )
    assert (
        table.estimate("missing-model", prompt_tokens=1, completion_tokens=1, cache="miss") is None
    )


async def test_chat_writes_a_request_log_and_metrics(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'llmgate.db'}"
    app = create_app(
        build_settings(database_url),
        redis_client=FakeAsyncRedis(decode_responses=True),
    )
    provider = Script([_response("Hello")])
    async with app.router.lifespan_context(app):
        app.state.gateway.pipeline.sleep = _no_sleep
        app.state.gateway.providers["openai"] = provider
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            key = await client.post(
                "/admin/keys",
                headers={"authorization": f"Bearer {MASTER_KEY}"},
                json={"name": "obs"},
            )
            raw = key.json()["key"]
            chat = await client.post(
                "/v1/chat/completions",
                headers=auth_header(raw),
                json=chat_body("gpt-4o"),
            )
            logs = await client.get(
                "/admin/logs",
                headers={"authorization": f"Bearer {MASTER_KEY}"},
            )
            usage = await client.get(
                "/admin/usage",
                headers={"authorization": f"Bearer {MASTER_KEY}"},
            )
            metrics = await client.get("/metrics")
            audit = await client.get(
                "/admin/audit",
                headers={"authorization": f"Bearer {MASTER_KEY}"},
            )
    assert chat.status_code == 200, chat.text
    assert logs.status_code == 200, logs.text
    assert logs.json()["data"]
    row = logs.json()["data"][0]
    assert row["model"] == "gpt-4o"
    assert row["total_tokens"] == 2
    assert row["estimated_cost_usd"] is not None
    assert usage.json()["requests"] >= 1
    assert metrics.status_code == 200
    assert b"llmgate_requests_total" in metrics.content
    assert any(item["action"] == "create" for item in audit.json()["data"])


async def test_viewer_cannot_create_keys(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'llmgate.db'}"
    app = create_app(
        build_settings(database_url),
        redis_client=FakeAsyncRedis(decode_responses=True),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            created = await client.post(
                "/admin/users",
                headers={"authorization": f"Bearer {MASTER_KEY}"},
                json={"email": "viewer@example.com", "role": "viewer"},
            )
            assert created.status_code == 201, created.text
            token = created.json()["token"]
            me = await client.get("/admin/me", headers=auth_header(token))
            denied = await client.post(
                "/admin/keys",
                headers=auth_header(token),
                json={"name": "nope"},
            )
            allowed = await client.get("/admin/keys", headers=auth_header(token))
            routing = await client.get("/admin/routing", headers=auth_header(token))
    assert me.json()["role"] == "viewer"
    assert denied.status_code == 400
    assert denied.json()["error"]["code"] == "forbidden"
    assert allowed.status_code == 200
    assert routing.status_code == 200
    assert any(item["id"] == "auto" for item in routing.json()["models"])
