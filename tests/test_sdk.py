"""The official OpenAI SDK pointed at LLMGate, with the upstream call mocked."""

from __future__ import annotations

import json

import httpx
import pytest
from openai import APIStatusError, AsyncOpenAI

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def _upstream(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode())
    if body.get("stream"):
        sse = (
            'data: {"id":"chatcmpl-up","object":"chat.completion.chunk","created":1,'
            '"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},'
            '"finish_reason":null}]}\n\n'
            'data: {"id":"chatcmpl-up","object":"chat.completion.chunk","created":1,'
            '"model":"gpt-4o-mini","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=sse)
    return httpx.Response(
        200,
        json={
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
        },
    )


async def test_openai_sdk_non_streaming_and_streaming(
    client: httpx.AsyncClient,
    api_key: str,
    respx_mock: object,
) -> None:
    respx_mock.route(host="testserver").pass_through()  # type: ignore[attr-defined]
    respx_mock.post(OPENAI_URL).mock(side_effect=_upstream)  # type: ignore[attr-defined]
    sdk = AsyncOpenAI(
        base_url="http://testserver/v1",
        api_key=api_key,
        http_client=client,
        max_retries=0,
    )

    completion = await sdk.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Hi"}],
    )
    assert completion.choices[0].message.content == "Hello"
    assert completion.usage is not None
    assert completion.usage.total_tokens == 6

    stream = await sdk.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Hi"}],
        stream=True,
    )
    parts: list[str] = []
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            parts.append(chunk.choices[0].delta.content)
    assert "".join(parts) == "Hello"


async def test_sdk_rejects_a_bad_gateway_key(client: httpx.AsyncClient) -> None:
    sdk = AsyncOpenAI(
        base_url="http://testserver/v1",
        api_key="sk-lg-invalid",
        http_client=client,
        max_retries=0,
    )
    with pytest.raises(APIStatusError) as exc:
        await sdk.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Hi"}],
        )
    assert exc.value.status_code == 401
