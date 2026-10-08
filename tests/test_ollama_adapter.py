from __future__ import annotations

import json

import httpx
import pytest

from llmgate.core.errors import InvalidRequestError
from llmgate.core.schemas import ChatCompletionRequest
from llmgate.providers.ollama import OllamaProvider

URL = "http://ollama.test/api/chat"


def _request(**extra: object) -> ChatCompletionRequest:
    payload: dict[str, object] = {
        "model": "local-alias",
        "messages": [
            {"role": "system", "content": "Be brief"},
            {"role": "user", "content": "Hello"},
        ],
        "temperature": 0.1,
        "max_tokens": 8,
    }
    payload.update(extra)
    return ChatCompletionRequest.model_validate(payload)


@pytest.fixture
async def provider() -> OllamaProvider:
    async with httpx.AsyncClient() as http:
        yield OllamaProvider("http://ollama.test", http)


async def test_complete_uses_native_chat(provider: OllamaProvider, respx_mock: object) -> None:
    route = respx_mock.post(URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(
            200,
            json={
                "model": "llama3.2",
                "message": {"role": "assistant", "content": "Hey"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 9,
                "eval_count": 1,
            },
        )
    )
    response = await provider.complete(_request(), "llama3.2")
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "llama3.2"
    assert sent["stream"] is False
    assert sent["messages"][0] == {"role": "system", "content": "Be brief"}
    assert sent["options"] == {"temperature": 0.1, "num_predict": 8}
    assert response.model == "local-alias"
    assert response.choices[0].message.content == "Hey"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 9
    assert response.usage.completion_tokens == 1
    assert response.usage.total_tokens == 10


async def test_stream_ndjson(provider: OllamaProvider, respx_mock: object) -> None:
    body = (
        '{"message":{"role":"assistant","content":"He"},"done":false}\n'
        '{"message":{"role":"assistant","content":"y"},"done":false}\n'
        '{"message":{"role":"assistant","content":""},"done":true,"done_reason":"length",'
        '"prompt_eval_count":4,"eval_count":2}\n'
    )
    respx_mock.post(URL).mock(return_value=httpx.Response(200, text=body))  # type: ignore[attr-defined]
    chunks = [chunk async for chunk in provider.stream(_request(stream=True), "llama3.2")]
    assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks) == "Hey"
    assert chunks[0].choices[0].delta.role == "assistant"
    assert chunks[-1].choices[0].finish_reason == "length"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 6


async def test_model_not_found(provider: OllamaProvider, respx_mock: object) -> None:
    respx_mock.post(URL).mock(  # type: ignore[attr-defined]
        return_value=httpx.Response(404, json={"error": "model 'missing' not found"})
    )
    with pytest.raises(InvalidRequestError) as exc:
        await provider.complete(_request(), "missing")
    assert exc.value.status_code == 400
    assert exc.value.code == "model_not_found"
