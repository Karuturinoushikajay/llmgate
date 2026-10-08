from __future__ import annotations

import json

import httpx
import pytest

from llmgate.core.errors import InvalidRequestError, RateLimitError
from llmgate.core.schemas import ChatCompletionRequest
from llmgate.providers.anthropic import AnthropicProvider

URL = "https://api.anthropic.com/v1/messages"


def _request(**extra: object) -> ChatCompletionRequest:
    payload: dict[str, object] = {
        "model": "claude-alias",
        "messages": [
            {"role": "system", "content": "You are A"},
            {"role": "developer", "content": "Be brief"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Again"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Look"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            },
        ],
        "max_tokens": 32,
        "stop": "END",
    }
    payload.update(extra)
    return ChatCompletionRequest.model_validate(payload)


@pytest.fixture
async def provider() -> AnthropicProvider:
    async with httpx.AsyncClient() as http:
        yield AnthropicProvider("sk-ant", "https://api.anthropic.com", http)


async def test_complete_translates_messages_and_usage(
    provider: AnthropicProvider,
    respx_mock: object,
) -> None:
    route = respx_mock.post(URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello back"}],
                "model": "claude-3-5-sonnet-20241022",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 11, "output_tokens": 2},
            },
        )
    )
    response = await provider.complete(_request(), "claude-3-5-sonnet-20241022")
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "claude-3-5-sonnet-20241022"
    assert sent["system"] == "You are A\n\nBe brief"
    assert sent["max_tokens"] == 32
    assert sent["stop_sequences"] == ["END"]
    assert sent["messages"][0] == {"role": "user", "content": "Hello"}
    assert sent["messages"][1] == {"role": "assistant", "content": "Hi"}
    merged = sent["messages"][2]
    assert merged["role"] == "user"
    assert merged["content"][0] == {"type": "text", "text": "Again"}
    assert merged["content"][1]["type"] == "text"
    assert merged["content"][2]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "AAAA",
    }
    assert route.calls.last.request.headers["x-api-key"] == "sk-ant"
    assert route.calls.last.request.headers["anthropic-version"] == "2023-06-01"
    assert response.model == "claude-alias"
    assert response.choices[0].message.content == "Hello back"
    assert response.choices[0].finish_reason == "stop"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 2
    assert response.usage.total_tokens == 13


async def test_stream_events_become_openai_chunks(
    provider: AnthropicProvider,
    respx_mock: object,
) -> None:
    sse = "\n".join(
        [
            "event: message_start",
            'data: {"type":"message_start","message":{"id":"msg_01","usage":{"input_tokens":8,"output_tokens":0}}}',
            "",
            "event: content_block_delta",
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}',
            "",
            "event: content_block_delta",
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}',
            "",
            "event: message_delta",
            'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":4}}',
            "",
            "event: message_stop",
            'data: {"type":"message_stop"}',
            "",
        ]
    )
    respx_mock.post(URL).mock(return_value=httpx.Response(200, text=sse))  # type: ignore[attr-defined]
    chunks = [
        chunk
        async for chunk in provider.stream(_request(stream=True), "claude-3-5-sonnet-20241022")
    ]
    text = "".join(chunk.choices[0].delta.content or "" for chunk in chunks)
    assert text == "Hello"
    assert chunks[0].choices[0].delta.role == "assistant"
    assert chunks[-1].choices[0].finish_reason == "length"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.prompt_tokens == 8
    assert chunks[-1].usage.completion_tokens == 4
    assert chunks[-1].id == "msg_01"


async def test_rate_limit(provider: AnthropicProvider, respx_mock: object) -> None:
    respx_mock.post(URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            429,
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
        )
    )
    with pytest.raises(RateLimitError) as exc:
        await provider.complete(_request(), "claude")
    assert exc.value.status_code == 429
    assert "slow down" in exc.value.message


async def test_system_only_is_rejected(provider: AnthropicProvider) -> None:
    request = ChatCompletionRequest.model_validate(
        {"model": "claude-alias", "messages": [{"role": "system", "content": "only"}]}
    )
    with pytest.raises(InvalidRequestError):
        await provider.complete(request, "claude")
