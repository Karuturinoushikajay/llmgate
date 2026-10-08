from __future__ import annotations

import json

import httpx
import pytest

from llmgate.core.errors import ProviderError, ProviderNotConfiguredError
from llmgate.core.schemas import ChatCompletionRequest
from llmgate.providers.openai import OpenAIProvider

URL = "https://api.openai.com/v1/chat/completions"


def _request(**extra: object) -> ChatCompletionRequest:
    payload: dict[str, object] = {
        "model": "alias-mini",
        "messages": [
            {"role": "system", "content": "Be brief"},
            {"role": "user", "content": "Hello"},
        ],
        "temperature": 0.2,
        "max_tokens": 16,
    }
    payload.update(extra)
    return ChatCompletionRequest.model_validate(payload)


@pytest.fixture
async def provider() -> OpenAIProvider:
    async with httpx.AsyncClient() as http:
        yield OpenAIProvider("sk-upstream", "https://api.openai.com/v1", http)


async def test_complete_translates_response(provider: OpenAIProvider, respx_mock: object) -> None:
    route = respx_mock.post(URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-up",
                "object": "chat.completion",
                "created": 10,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
            },
        )
    )
    response = await provider.complete(_request(), "gpt-4o-mini")
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "gpt-4o-mini"
    assert sent["messages"][0] == {"role": "system", "content": "Be brief"}
    assert sent["temperature"] == 0.2
    assert sent["max_tokens"] == 16
    assert sent["stream"] is False
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-upstream"
    assert response.model == "alias-mini"
    assert response.choices[0].message.content == "Hi"
    assert response.usage is not None
    assert response.usage.total_tokens == 5


async def test_stream_normalizes_chunks(provider: OpenAIProvider, respx_mock: object) -> None:
    sse = (
        'data: {"id":"chatcmpl-up","object":"chat.completion.chunk","created":10,'
        '"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},'
        '"finish_reason":null}]}\n\n'
        'data: {"id":"chatcmpl-up","object":"chat.completion.chunk","created":10,'
        '"model":"gpt-4o-mini","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n'
        "data: [DONE]\n\n"
    )
    respx_mock.post(URL).mock(return_value=httpx.Response(200, text=sse))  # type: ignore[attr-defined]
    chunks = [chunk async for chunk in provider.stream(_request(stream=True), "gpt-4o-mini")]
    assert chunks[0].choices[0].delta.content == "Hi"
    assert chunks[0].model == "alias-mini"
    assert chunks[-1].choices[0].finish_reason == "stop"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 4


async def test_upstream_auth_failure_is_bad_gateway(
    provider: OpenAIProvider, respx_mock: object
) -> None:
    respx_mock.post(URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    with pytest.raises(ProviderError) as exc:
        await provider.complete(_request(), "gpt-4o-mini")
    assert exc.value.status_code == 502


async def test_missing_credential() -> None:
    async with httpx.AsyncClient() as http:
        provider = OpenAIProvider("", "https://api.openai.com/v1", http)
        with pytest.raises(ProviderNotConfiguredError):
            await provider.complete(_request(), "gpt-4o-mini")
