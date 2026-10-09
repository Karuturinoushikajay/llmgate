import httpx


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["postgres"] == "ok"
    assert body["redis"] == "ok"
    assert body["providers"]["openai"] is True
    assert body["providers"]["ollama"] is True


async def test_root(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json()["name"] == "LLMGate"
