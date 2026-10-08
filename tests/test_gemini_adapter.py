from __future__ import annotations

import json

import httpx
import pytest

from llmgate.core.schemas import ChatCompletionRequest
from llmgate.providers.gemini import GeminiProvider

URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"


def _request(**extra: object) -> ChatCompletionRequest:
    payload: dict[str, object] = {
        "model": "gemini-alias",
        "messages": [
            {"role": "system", "content": "Be brief"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Next"},
        ],
        "temperature": 0.4,
        "max_tokens": 20,
    }
    payload.update(extra)
    return ChatCompletionRequest.model_validate(payload)


@pytest.fixture
async def provider() -> GeminiProvider:
    async with httpx.AsyncClient() as http:
        yield GeminiProvider("gem-key", "https://generativelanguage.googleapis.com", http)


async def test_complete_maps_roles_and_usage(provider: GeminiProvider, respx_mock: object) -> None:
    route = respx_mock.post(URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "Hello"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 6,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 7,
                },
            },
        )
    )
    response = await provider.complete(_request(), "gemini-2.0-flash")
    sent = json.loads(route.calls.last.request.content)
    assert sent["systemInstruction"] == {"parts": [{"text": "Be brief"}]}
    assert sent["contents"][0] == {"role": "user", "parts": [{"text": "Hello"}]}
    assert sent["contents"][1] == {"role": "model", "parts": [{"text": "Hi"}]}
    assert sent["contents"][2] == {"role": "user", "parts": [{"text": "Next"}]}
    assert sent["generationConfig"]["temperature"] == 0.4
    assert sent["generationConfig"]["maxOutputTokens"] == 20
    assert route.calls.last.request.headers["x-goog-api-key"] == "gem-key"
    assert response.model == "gemini-alias"
    assert response.choices[0].message.content == "Hello"
    assert response.choices[0].finish_reason == "stop"
    assert response.usage is not None
    assert response.usage.total_tokens == 7


async def test_stream_sse(provider: GeminiProvider, respx_mock: object) -> None:
    stream_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:streamGenerateContent?alt=sse"
    )
    sse = (
        'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Hel"}]}}]}\n\n'
        'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"lo"}]},'
        '"finishReason":"MAX_TOKENS"}],"usageMetadata":{"promptTokenCount":3,'
        '"candidatesTokenCount":2,"totalTokenCount":5}}\n\n'
    )
    respx_mock.post(stream_url).mock(return_value=httpx.Response(200, text=sse))  # type: ignore[attr-defined]
    chunks = [chunk async for chunk in provider.stream(_request(stream=True), "gemini-2.0-flash")]
    assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks) == "Hello"
    assert chunks[-1].choices[0].finish_reason == "length"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.completion_tokens == 2


async def test_prompt_block_is_content_filter(provider: GeminiProvider, respx_mock: object) -> None:
    respx_mock.post(URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            200,
            json={"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}},
        )
    )
    response = await provider.complete(_request(), "gemini-2.0-flash")
    assert response.choices[0].finish_reason == "content_filter"
    assert response.choices[0].message.content == ""
