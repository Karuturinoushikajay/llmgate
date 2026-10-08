from __future__ import annotations

import httpx
import structlog

from tests.helpers import auth_header, chat_body, parse_sse

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def _completion() -> dict[str, object]:
    return {
        "id": "chatcmpl-up",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }


async def test_non_streaming_chat(
    client: httpx.AsyncClient,
    api_key: str,
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    route = respx_mock.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=_completion()))  # type: ignore[attr-defined]
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_header(api_key),
        json=chat_body("gpt-4o-mini"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "gpt-4o-mini"
    assert body["choices"][0]["message"]["content"] == "Hello"
    assert body["usage"]["total_tokens"] == 6
    assert route.calls.last.request.headers["authorization"] == "Bearer test-openai"


async def test_prefixed_model_and_streaming_done_marker(
    client: httpx.AsyncClient,
    api_key: str,
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    sse = (
        'data: {"id":"chatcmpl-up","object":"chat.completion.chunk","created":1,'
        '"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"Yo"},"finish_reason":null}]}\n\n'
        'data: {"id":"chatcmpl-up","object":"chat.completion.chunk","created":1,'
        '"model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\n'
        "data: [DONE]\n\n"
    )
    route = respx_mock.post(OPENAI_URL).mock(return_value=httpx.Response(200, text=sse))  # type: ignore[attr-defined]
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_header(api_key),
        json=chat_body(
            "openai/gpt-4o",
            stream=True,
            stream_options={"include_usage": True},
        ),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert events[-1] == "[DONE]"
    assert isinstance(events[0], dict)
    assert events[0]["choices"][0]["delta"]["content"] == "Yo"
    assert events[0]["model"] == "openai/gpt-4o"
    usage_events = [event for event in events if isinstance(event, dict) and "usage" in event]
    assert usage_events[-1]["usage"]["total_tokens"] == 3
    sent = route.calls.last.request.read().decode()
    assert '"model": "gpt-4o"' in sent or '"model":"gpt-4o"' in sent


async def test_streaming_access_log_records_tokens(
    client: httpx.AsyncClient,
    api_key: str,
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    sse = (
        'data: {"id":"chatcmpl-up","object":"chat.completion.chunk","created":1,'
        '"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"content":"Yo"},"finish_reason":null}]}\n\n'
        'data: {"id":"chatcmpl-up","object":"chat.completion.chunk","created":1,'
        '"model":"gpt-4o-mini","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\n'
        "data: [DONE]\n\n"
    )
    respx_mock.post(OPENAI_URL).mock(return_value=httpx.Response(200, text=sse))  # type: ignore[attr-defined]
    with structlog.testing.capture_logs() as logs:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_header(api_key),
            json=chat_body("gpt-4o-mini", stream=True),
        )
    assert response.status_code == 200
    completed = [
        event
        for event in logs
        if event.get("event") == "request.completed" and event.get("path") == "/v1/chat/completions"
    ]
    assert completed
    assert completed[-1]["prompt_tokens"] == 2
    assert completed[-1]["completion_tokens"] == 1
    assert completed[-1]["total_tokens"] == 3
    assert completed[-1]["stream"] is True


async def test_unknown_model(client: httpx.AsyncClient, api_key: str) -> None:
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_header(api_key),
        json=chat_body("no-such-model"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


async def test_n_greater_than_one(client: httpx.AsyncClient, api_key: str) -> None:
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_header(api_key),
        json=chat_body("gpt-4o-mini", n=2),
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "n"


async def test_tools_rejected_for_anthropic(client: httpx.AsyncClient, api_key: str) -> None:
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_header(api_key),
        json=chat_body(
            "claude-3-5-sonnet",
            tools=[{"type": "function", "function": {"name": "ping"}}],
        ),
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "tools"


async def test_upstream_429(client: httpx.AsyncClient, api_key: str, respx_mock: object) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    respx_mock.post(OPENAI_URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(429, json={"error": {"message": "slow down"}})
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_header(api_key),
        json=chat_body("gpt-4o-mini"),
    )
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"


async def test_validation_error_is_openai_shaped(client: httpx.AsyncClient, api_key: str) -> None:
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_header(api_key),
        json={"model": "gpt-4o-mini"},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["param"] == "messages"
