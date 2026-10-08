"""Serialize chat chunks as OpenAI server-sent events."""

from __future__ import annotations

import json
from typing import Any

from llmgate.core.errors import GatewayError
from llmgate.core.schemas import ChatCompletionChunk

_DONE = "data: [DONE]\n\n"


def format_chunk(chunk: ChatCompletionChunk, *, include_usage: bool) -> str:
    payload = _chunk_payload(chunk, include_usage=include_usage)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def format_done() -> str:
    return _DONE


def format_error(exc: GatewayError) -> str:
    return f"data: {json.dumps(exc.to_body(), ensure_ascii=False)}\n\n"


def should_emit(chunk: ChatCompletionChunk, *, include_usage: bool) -> bool:
    if not chunk.choices:
        return include_usage and chunk.usage is not None
    choice = chunk.choices[0]
    delta = choice.delta
    has_delta = delta.role is not None or delta.content is not None or bool(delta.tool_calls)
    if has_delta or choice.finish_reason is not None:
        return True
    return include_usage and chunk.usage is not None


def _chunk_payload(chunk: ChatCompletionChunk, *, include_usage: bool) -> dict[str, Any]:
    choices: list[dict[str, Any]] = []
    for choice in chunk.choices:
        delta: dict[str, Any] = {}
        if choice.delta.role is not None:
            delta["role"] = choice.delta.role
        if choice.delta.content is not None:
            delta["content"] = choice.delta.content
        if choice.delta.tool_calls is not None:
            delta["tool_calls"] = choice.delta.tool_calls
        choices.append(
            {
                "index": choice.index,
                "delta": delta,
                "finish_reason": choice.finish_reason,
            }
        )
    payload: dict[str, Any] = {
        "id": chunk.id,
        "object": "chat.completion.chunk",
        "created": chunk.created,
        "model": chunk.model,
        "choices": choices,
    }
    if include_usage and chunk.usage is not None:
        payload["usage"] = chunk.usage.model_dump()
    return payload
