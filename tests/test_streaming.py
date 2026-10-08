"""Streaming translations through the HTTP route, one provider at a time."""

from __future__ import annotations

import httpx
from tests.helpers import auth_header, chat_body, parse_sse


async def test_anthropic_stream_through_gateway(
    client: httpx.AsyncClient,
    api_key: str,
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    sse = "\n".join(
        [
            'data: {"type":"message_start","message":{"id":"msg_9","usage":{"input_tokens":3,"output_tokens":0}}}',
            "",
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}',
            "",
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}',
            "",
            'data: {"type":"message_stop"}',
            "",
        ]
    )
    respx_mock.post("https://api.anthropic.com/v1/messages").mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(200, text=sse)
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_header(api_key),
        json=chat_body("claude-3-5-sonnet", stream=True),
    )
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    assert events[-1] == "[DONE]"
    text = "".join(
        event["choices"][0]["delta"].get("content", "")
        for event in events
        if isinstance(event, dict) and event["choices"]
    )
    assert text == "Hi"
    finish = [event for event in events if isinstance(event, dict) and event["choices"]][-1]
    assert finish["choices"][0]["finish_reason"] == "stop"
    # Usage stays in the log, not the SSE body, unless the caller asked for it.
    assert all("usage" not in event for event in events if isinstance(event, dict))


async def test_ollama_stream_through_gateway(
    client: httpx.AsyncClient,
    api_key: str,
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    body = (
        '{"message":{"role":"assistant","content":"Yo"},"done":false}\n'
        '{"message":{"role":"assistant","content":""},"done":true,"done_reason":"stop",'
        '"prompt_eval_count":2,"eval_count":1}\n'
    )
    respx_mock.post("http://ollama.test/api/chat").mock(return_value=httpx.Response(200, text=body))  # type: ignore[attr-defined]
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_header(api_key),
        json=chat_body("llama3.2", stream=True),
    )
    events = parse_sse(response.text)
    assert events[-1] == "[DONE]"
    assert events[0]["choices"][0]["delta"]["content"] == "Yo"  # type: ignore[index]


async def test_gemini_stream_through_gateway(
    client: httpx.AsyncClient,
    api_key: str,
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    sse = 'data: {"candidates":[{"content":{"parts":[{"text":"Hey"}],"role":"model"},"finishReason":"STOP"}]}\n\n'
    respx_mock.post(  # type: ignore[attr-defined]
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:streamGenerateContent?alt=sse"
    ).mock(return_value=httpx.Response(200, text=sse))
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_header(api_key),
        json=chat_body("gemini-2.0-flash", stream=True),
    )
    events = parse_sse(response.text)
    assert any(
        isinstance(event, dict)
        and event["choices"]
        and event["choices"][0]["delta"].get("content") == "Hey"
        for event in events
    )
    assert events[-1] == "[DONE]"


async def test_upstream_error_before_tokens_is_json(
    client: httpx.AsyncClient,
    api_key: str,
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    respx_mock.post(  # type: ignore[attr-defined]
        "https://api.anthropic.com/v1/messages"
    ).mock(return_value=httpx.Response(400, json={"error": {"message": "bad request"}}))
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_header(api_key),
        json=chat_body("claude-3-5-sonnet", stream=True),
    )
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["message"] == "bad request"
