"""HTTP-level checks: a failing upstream falls over, the breaker opens, limits return 429."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import httpx

from tests.helpers import MASTER_KEY, auth_header, chat_body, parse_sse

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def _yaml(path: Path, body: str) -> str:
    path.write_text(body, encoding="utf-8")
    return str(path)


def _completion(content: str = "Hello") -> dict[str, object]:
    return {
        "id": "chatcmpl-up",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }


def _anthropic(content: str = "from-anthropic") -> dict[str, object]:
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }


async def _key(client: httpx.AsyncClient, **limits: int) -> str:
    response = await client.post(
        "/admin/keys",
        headers={"authorization": f"Bearer {MASTER_KEY}"},
        json={"name": "limited", **limits},
    )
    assert response.status_code == 201, response.text
    key = response.json()["key"]
    assert isinstance(key, str)
    return key


async def test_fallback_serves_and_names_the_provider(
    make_client: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]],
    tmp_path: Path,
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    registry = _yaml(
        tmp_path / "models.yaml",
        "models:\n"
        "  - id: routed\n"
        "    provider: openai\n"
        "    upstream_model: gpt-4o-mini\n"
        "    fallbacks:\n"
        "      - {provider: anthropic, upstream_model: claude-3-5-haiku-20241022}\n",
    )
    respx_mock.post(OPENAI_URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(500, json={"error": {"message": "unavailable"}})
    )
    respx_mock.post(ANTHROPIC_URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, json=_anthropic())
    )
    async with make_client(
        models_path=registry,
        retry_max_attempts=2,
        retry_base_delay_seconds=0,
        breaker_failure_threshold=10,
    ) as client:
        key = await _key(client)
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("routed"),
        )
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "from-anthropic"
    assert response.headers["x-llmgate-provider"] == "anthropic"
    assert response.headers["x-llmgate-model"] == "claude-3-5-haiku-20241022"
    assert response.headers["x-ratelimit-limit-requests"] == "60"


async def test_open_breaker_stops_calling_the_upstream(
    make_client: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]],
    tmp_path: Path,
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    registry = _yaml(
        tmp_path / "models.yaml",
        "models:\n  - {id: solo, provider: openai, upstream_model: gpt-4o-mini}\n",
    )
    route = respx_mock.post(OPENAI_URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(500, json={"error": {"message": "down"}})
    )
    async with make_client(
        models_path=registry,
        retry_max_attempts=1,
        breaker_failure_threshold=1,
        breaker_cooldown_seconds=3600,
    ) as client:
        key = await _key(client)
        first = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("solo"),
        )
        second = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("solo"),
        )
    assert first.status_code == 502
    assert second.status_code == 503
    assert second.json()["error"]["code"] == "circuit_open"
    assert route.call_count == 1


async def test_rate_limit_429_uses_openai_headers(
    make_client: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]],
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    respx_mock.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=_completion()))  # type: ignore[attr-defined]
    async with make_client(retry_max_attempts=1) as client:
        key = await _key(client, requests_per_minute=2, tokens_per_minute=0)
        headers = auth_header(key)
        assert (
            await client.post(
                "/v1/chat/completions", headers=headers, json=chat_body("gpt-4o-mini")
            )
        ).status_code == 200
        assert (
            await client.post(
                "/v1/chat/completions", headers=headers, json=chat_body("gpt-4o-mini")
            )
        ).status_code == 200
        denied = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json=chat_body("gpt-4o-mini"),
        )
    assert denied.status_code == 429
    body = denied.json()["error"]
    assert body["type"] == "rate_limit_error"
    assert body["code"] == "rate_limit_exceeded"
    assert denied.headers["retry-after"].isdigit()
    assert denied.headers["x-ratelimit-limit-requests"] == "2"
    assert denied.headers["x-ratelimit-remaining-requests"] == "0"
    assert denied.headers["x-ratelimit-reset-requests"]


async def test_stream_retries_a_failure_before_the_first_chunk(
    make_client: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]],
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    sse = (
        'data: {"id":"chatcmpl-up","object":"chat.completion.chunk","created":1,'
        '"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"content":"Yo"},"finish_reason":null}]}\n\n'
        "data: [DONE]\n\n"
    )
    attempts = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        return httpx.Response(200, text=sse)

    respx_mock.post(OPENAI_URL).mock(side_effect=respond)  # type: ignore[attr-defined]
    async with make_client(retry_max_attempts=2, retry_base_delay_seconds=0) as client:
        key = await _key(client, tokens_per_minute=0)
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o-mini", stream=True),
        )
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert events[0]["choices"][0]["delta"]["content"] == "Yo"  # type: ignore[index]
    assert events[-1] == "[DONE]"
    assert attempts["n"] == 2
    assert response.headers["x-llmgate-provider"] == "openai"


async def test_non_retryable_400_is_a_single_attempt(
    make_client: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]],
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    route = respx_mock.post(OPENAI_URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(400, json={"error": {"message": "bad request"}})
    )
    async with make_client(retry_max_attempts=3, retry_base_delay_seconds=0) as client:
        key = await _key(client)
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_header(key),
            json=chat_body("gpt-4o-mini"),
        )
    assert response.status_code == 400
    assert route.call_count == 1
