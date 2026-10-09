"""OpenAI chat completions adapter.

The request is already in OpenAI's shape. This adapter still rebuilds the
response so aliases, usage, and errors leave the gateway in one schema.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx

from llmgate.core.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    new_completion_id,
    unix_timestamp,
)
from llmgate.core.usage import usage_from_openai
from llmgate.providers.http import (
    assign_if_set,
    iter_sse_json,
    raise_for_status,
    require_credentials,
)


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, base_url: str, http: httpx.AsyncClient) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.http = http

    async def complete(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
    ) -> ChatCompletionResponse:
        require_credentials(self.name, self.api_key)
        response = await self.http.post(
            self._url(),
            json=self._payload(request, upstream_model, stream=False),
            headers=self._headers(),
        )
        if response.status_code >= 400:
            raise_for_status(self.name, response.status_code, response.content)
        return self._response_from_upstream(response.json(), request.model)

    async def stream(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
    ) -> AsyncGenerator[ChatCompletionChunk, None]:
        require_credentials(self.name, self.api_key)
        async with self.http.stream(
            "POST",
            self._url(),
            json=self._payload(request, upstream_model, stream=True),
            headers=self._headers(),
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise_for_status(self.name, response.status_code, body)
            async for payload in iter_sse_json(response):
                yield self._chunk_from_upstream(payload, request.model)

    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }

    def _payload(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": upstream_model,
            "messages": [
                message.model_dump(exclude_none=True, mode="json") for message in request.messages
            ],
            "stream": stream,
        }
        assign_if_set(payload, "temperature", request.temperature)
        assign_if_set(payload, "top_p", request.top_p)
        assign_if_set(payload, "stop", request.stop)
        assign_if_set(payload, "presence_penalty", request.presence_penalty)
        assign_if_set(payload, "frequency_penalty", request.frequency_penalty)
        assign_if_set(payload, "user", request.user)
        assign_if_set(payload, "tools", request.tools)
        assign_if_set(payload, "tool_choice", request.tool_choice)
        assign_if_set(payload, "response_format", request.response_format)
        assign_if_set(payload, "seed", request.seed)
        if request.max_completion_tokens is not None:
            payload["max_completion_tokens"] = request.max_completion_tokens
        elif request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if stream:
            # Always ask upstream for usage so the access log can record it.
            # The HTTP layer drops it unless the caller set include_usage.
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _response_from_upstream(
        self,
        payload: dict[str, Any],
        public_model: str,
    ) -> ChatCompletionResponse:
        normalized = dict(payload)
        normalized["model"] = public_model
        normalized.setdefault("id", new_completion_id())
        normalized.setdefault("created", unix_timestamp())
        normalized.setdefault("object", "chat.completion")
        response = ChatCompletionResponse.model_validate(normalized)
        if response.usage is None:
            response.usage = usage_from_openai(None)
        return response

    def _chunk_from_upstream(
        self,
        payload: dict[str, Any],
        public_model: str,
    ) -> ChatCompletionChunk:
        normalized = dict(payload)
        normalized["model"] = public_model
        normalized.setdefault("id", new_completion_id())
        normalized.setdefault("created", unix_timestamp())
        normalized.setdefault("object", "chat.completion.chunk")
        return ChatCompletionChunk.model_validate(normalized)
