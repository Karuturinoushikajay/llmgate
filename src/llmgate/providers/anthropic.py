"""Anthropic Messages API adapter."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx

from llmgate.core.errors import InvalidRequestError, ProviderError
from llmgate.core.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
    completion_chunk,
    completion_response,
    new_completion_id,
    unix_timestamp,
)
from llmgate.providers.content import anthropic_blocks, message_text
from llmgate.providers.http import (
    assign_if_set,
    iter_sse_json,
    raise_for_status,
    request_timeout,
    require_credentials,
)

_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 4096
_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}
# Anthropic requires the transcript to start with a user turn.
_CONTINUATION = "Continue."


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, base_url: str, http: httpx.AsyncClient) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.http = http

    async def complete(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        *,
        upstream_timeout: httpx.Timeout | None = None,
    ) -> ChatCompletionResponse:
        require_credentials(self.name, self.api_key)
        response = await self.http.post(
            self._url(),
            json=self._payload(request, upstream_model, stream=False),
            headers=self._headers(),
            timeout=request_timeout(upstream_timeout),
        )
        if response.status_code >= 400:
            raise_for_status(self.name, response.status_code, response.content, response.headers)
        return self._response_from_upstream(response.json(), request.model)

    async def stream(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        *,
        upstream_timeout: httpx.Timeout | None = None,
    ) -> AsyncGenerator[ChatCompletionChunk, None]:
        require_credentials(self.name, self.api_key)
        completion_id = new_completion_id()
        created = unix_timestamp()
        prompt_tokens = 0
        completion_tokens = 0
        finish_reason = "stop"
        started = False
        async with self.http.stream(
            "POST",
            self._url(),
            json=self._payload(request, upstream_model, stream=True),
            headers=self._headers(),
            timeout=request_timeout(upstream_timeout),
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise_for_status(self.name, response.status_code, body, response.headers)
            async for event in iter_sse_json(response):
                event_type = event.get("type")
                if event_type == "message_start":
                    message = event.get("message") or {}
                    if isinstance(message, dict):
                        if isinstance(message.get("id"), str):
                            completion_id = message["id"]
                        usage = message.get("usage") or {}
                        if isinstance(usage, dict):
                            prompt_tokens = int(usage.get("input_tokens") or 0)
                            completion_tokens = int(usage.get("output_tokens") or 0)
                elif event_type == "content_block_delta":
                    delta = event.get("delta") or {}
                    if isinstance(delta, dict) and delta.get("type") == "text_delta":
                        text = str(delta.get("text") or "")
                        role = None
                        if not started:
                            role = "assistant"
                            started = True
                        if text or role:
                            yield completion_chunk(
                                completion_id=completion_id,
                                created=created,
                                model=request.model,
                                content=text or None,
                                role=role,
                            )
                elif event_type == "message_delta":
                    delta = event.get("delta") or {}
                    if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                        finish_reason = _map_finish(delta["stop_reason"])
                    usage = event.get("usage") or {}
                    if isinstance(usage, dict) and usage.get("output_tokens") is not None:
                        completion_tokens = int(usage["output_tokens"])
                elif event_type == "error":
                    error = event.get("error") or {}
                    message = "Anthropic stream failed"
                    if isinstance(error, dict) and isinstance(error.get("message"), str):
                        message = error["message"]
                    raise ProviderError(message)
        yield completion_chunk(
            completion_id=completion_id,
            created=created,
            model=request.model,
            finish_reason=finish_reason,
            role=None if started else "assistant",
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

    def _url(self) -> str:
        return f"{self.base_url}/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _payload(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        system, messages = _convert_messages(request.messages)
        payload: dict[str, Any] = {
            "model": upstream_model,
            "max_tokens": request.max_output_tokens() or _DEFAULT_MAX_TOKENS,
            "messages": messages,
            "stream": stream,
        }
        assign_if_set(payload, "system", system)
        assign_if_set(payload, "temperature", request.temperature)
        assign_if_set(payload, "top_p", request.top_p)
        assign_if_set(payload, "stop_sequences", request.stop_sequences())
        return payload

    def _response_from_upstream(
        self,
        payload: dict[str, Any],
        public_model: str,
    ) -> ChatCompletionResponse:
        blocks = payload.get("content") or []
        texts: list[str] = []
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
        raw_usage = payload.get("usage")
        usage_raw = raw_usage if isinstance(raw_usage, dict) else {}
        prompt = int(usage_raw.get("input_tokens") or 0)
        completion = int(usage_raw.get("output_tokens") or 0)
        stop_reason = payload.get("stop_reason")
        finish = _map_finish(stop_reason) if isinstance(stop_reason, str) else "stop"
        completion_id = payload.get("id")
        return completion_response(
            completion_id=completion_id if isinstance(completion_id, str) else new_completion_id(),
            created=unix_timestamp(),
            model=public_model,
            content="".join(texts),
            finish_reason=finish,
            usage=Usage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
            ),
        )


def _map_finish(stop_reason: str) -> str:
    return _FINISH_REASONS.get(stop_reason, "stop")


def _convert_messages(messages: list[ChatMessage]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role in {"system", "developer"}:
            text = message_text(message)
            if text:
                system_parts.append(text)
            continue
        if message.role not in {"user", "assistant"}:
            raise InvalidRequestError(
                f"Role '{message.role}' is not supported for anthropic",
                param="messages",
            )
        block = anthropic_blocks(message)
        if converted and converted[-1]["role"] == message.role:
            converted[-1]["content"] = _merge_content(converted[-1]["content"], block)
        else:
            converted.append({"role": message.role, "content": block})
    if not converted:
        raise InvalidRequestError("At least one user message is required", param="messages")
    if converted[0]["role"] != "user":
        converted.insert(0, {"role": "user", "content": _CONTINUATION})
    system = "\n\n".join(system_parts) or None
    return system, converted


def _merge_content(
    existing: str | list[dict[str, Any]],
    new: str | list[dict[str, Any]],
) -> str | list[dict[str, Any]]:
    if isinstance(existing, str) and isinstance(new, str):
        return f"{existing}\n{new}"
    return _as_blocks(existing) + _as_blocks(new)


def _as_blocks(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content
