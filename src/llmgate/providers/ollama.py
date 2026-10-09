"""Ollama native ``/api/chat`` adapter.

Ollama also exposes an OpenAI-compatible route. We call the native API so the
adapter has a real translation job and local models stay explicit in config.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from llmgate.core.errors import ProviderError
from llmgate.core.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    completion_chunk,
    completion_response,
    new_completion_id,
    unix_timestamp,
)
from llmgate.core.usage import usage_from_ollama
from llmgate.providers.content import ollama_message
from llmgate.providers.http import raise_for_status

_FINISH_REASONS = {
    "stop": "stop",
    "length": "length",
}


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http

    async def complete(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
    ) -> ChatCompletionResponse:
        response = await self.http.post(
            self._url(),
            json=self._payload(request, upstream_model, stream=False),
        )
        if response.status_code >= 400:
            raise_for_status(self.name, response.status_code, response.content)
        return self._response_from_upstream(response.json(), request.model)

    async def stream(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
    ) -> AsyncGenerator[ChatCompletionChunk, None]:
        completion_id = new_completion_id()
        created = unix_timestamp()
        started = False
        async with self.http.stream(
            "POST",
            self._url(),
            json=self._payload(request, upstream_model, stream=True),
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise_for_status(self.name, response.status_code, body)
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ProviderError("Ollama returned a non-JSON stream line") from exc
                if not isinstance(event, dict):
                    continue
                raw_message = event.get("message")
                message = raw_message if isinstance(raw_message, dict) else {}
                text = str(message.get("content") or "")
                done = bool(event.get("done"))
                if text:
                    yield completion_chunk(
                        completion_id=completion_id,
                        created=created,
                        model=request.model,
                        content=text,
                        role=None if started else "assistant",
                    )
                    started = True
                if done:
                    reason = event.get("done_reason")
                    finish = (
                        _FINISH_REASONS.get(reason, "stop") if isinstance(reason, str) else "stop"
                    )
                    yield completion_chunk(
                        completion_id=completion_id,
                        created=created,
                        model=request.model,
                        finish_reason=finish,
                        role=None if started else "assistant",
                        usage=usage_from_ollama(event),
                    )

    def _url(self) -> str:
        return f"{self.base_url}/api/chat"

    def _payload(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            role = "system" if message.role == "developer" else message.role
            messages.append(ollama_message(message, role=role))
        payload: dict[str, Any] = {
            "model": upstream_model,
            "messages": messages,
            "stream": stream,
        }
        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.top_p is not None:
            options["top_p"] = request.top_p
        max_tokens = request.max_output_tokens()
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        stop = request.stop_sequences()
        if stop:
            options["stop"] = stop
        if options:
            payload["options"] = options
        return payload

    def _response_from_upstream(
        self,
        payload: dict[str, Any],
        public_model: str,
    ) -> ChatCompletionResponse:
        raw_message = payload.get("message")
        message = raw_message if isinstance(raw_message, dict) else {}
        content = str(message.get("content") or "")
        reason = payload.get("done_reason")
        finish = _FINISH_REASONS.get(reason, "stop") if isinstance(reason, str) else "stop"
        return completion_response(
            completion_id=new_completion_id(),
            created=unix_timestamp(),
            model=public_model,
            content=content,
            finish_reason=finish,
            usage=usage_from_ollama(payload),
        )
