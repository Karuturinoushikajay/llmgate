from __future__ import annotations

import httpx
from tests.helpers import MASTER_KEY, auth_header


async def test_missing_key_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["type"] == "authentication_error"
    assert body["error"]["code"] == "invalid_api_key"


async def test_unknown_key_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models", headers=auth_header("sk-lg-nope"))
    assert response.status_code == 401


async def test_revoked_key_is_rejected(client: httpx.AsyncClient, api_key: str) -> None:
    created = await client.post(
        "/admin/keys",
        headers={"authorization": f"Bearer {MASTER_KEY}"},
        json={"name": "soon-revoked"},
    )
    key_id = created.json()["id"]
    raw = created.json()["key"]
    deleted = await client.delete(
        f"/admin/keys/{key_id}",
        headers={"authorization": f"Bearer {MASTER_KEY}"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["revoked_at"] is not None
    response = await client.get("/v1/models", headers=auth_header(raw))
    assert response.status_code == 401
    # The original fixture key still works.
    assert (await client.get("/v1/models", headers=auth_header(api_key))).status_code == 200


async def test_master_key_cannot_call_v1(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models", headers={"authorization": f"Bearer {MASTER_KEY}"})
    assert response.status_code == 401


async def test_gateway_key_cannot_manage_keys(client: httpx.AsyncClient, api_key: str) -> None:
    response = await client.get("/admin/keys", headers=auth_header(api_key))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_master_key"


async def test_list_keys_hides_secret(client: httpx.AsyncClient, api_key: str) -> None:
    response = await client.get("/admin/keys", headers={"authorization": f"Bearer {MASTER_KEY}"})
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload
    assert "key" not in payload[0]
    assert api_key not in response.text


async def test_last_used_updates(client: httpx.AsyncClient, api_key: str) -> None:
    await client.get("/v1/models", headers=auth_header(api_key))
    listed = await client.get("/admin/keys", headers={"authorization": f"Bearer {MASTER_KEY}"})
    assert listed.json()["data"][0]["last_used_at"] is not None


async def test_request_id_round_trip(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"x-request-id": "req-123"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-123"


async def test_models_lists_registry(client: httpx.AsyncClient, api_key: str) -> None:
    response = await client.get("/v1/models", headers=auth_header(api_key))
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert "gpt-4o-mini" in ids
    assert "llama3.2" in ids
    assert response.json()["object"] == "list"
