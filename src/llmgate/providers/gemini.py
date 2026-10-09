"""Google Gemini generateContent adapter."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote

import httpx

from llmgate.core.errors import InvalidRequestError
from llmgate.core.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    completion_chunk,
    completion_response,
    new_completion_id,
    unix_timestamp,
)
from llmgate.core.usage import usage_from_gemini
from llmgate.providers.content import gemini_parts, message_text
from llmgate.providers.http import iter_sse_json, raise_for_status, require_credentials

_FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "OTHER": "stop",
    "FINISH_REASON_UNSPECIFIED": "stop",
}
_CONTINUATION = "Continue."


class GeminiProvider:
    name = "gemini"

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
            self._url(upstream_model, stream=False),
            json=self._payload(request),
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
        completion_id = new_completion_id()
        created = unix_timestamp()
        started = False
        last_usage = usage_from_gemini(None)
        finish_reason: str | None = None
        async with self.http.stream(
            "POST",
            self._url(upstream_model, stream=True),
            json=self._payload(request),
            headers=self._headers(),
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise_for_status(self.name, response.status_code, body)
            async for event in iter_sse_json(response):
                usage = usage_from_gemini(_dict(event.get("usageMetadata")))
                if usage.total_tokens or usage.prompt_tokens or usage.completion_tokens:
                    last_usage = usage
                text, reason = _candidate_text(event)
                if reason:
                    finish_reason = reason
                if text:
                    role = None if started else "assistant"
                    started = True
                    yield completion_chunk(
                        completion_id=completion_id,
                        created=created,
                        model=request.model,
                        content=text,
                        role=role,
                    )
        yield completion_chunk(
            completion_id=completion_id,
            created=created,
            model=request.model,
            finish_reason=finish_reason or "stop",
            role=None if started else "assistant",
            usage=last_usage,
        )

    def _url(self, model: str, *, stream: bool) -> str:
        action = "streamGenerateContent" if stream else "generateContent"
        safe_model = quote(model, safe="")
        url = f"{self.base_url}/v1beta/models/{safe_model}:{action}"
        if stream:
            return url + "?alt=sse"
        return url

    def _headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self.api_key,
            "content-type": "application/json",
        }

    def _payload(self, request: ChatCompletionRequest) -> dict[str, Any]:
        system, contents = _convert_messages(request.messages)
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        generation: dict[str, Any] = {}
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.top_p is not None:
            generation["topP"] = request.top_p
        max_tokens = request.max_output_tokens()
        if max_tokens is not None:
            generation["maxOutputTokens"] = max_tokens
        stop = request.stop_sequences()
        if stop:
            generation["stopSequences"] = stop
        if generation:
            payload["generationConfig"] = generation
        return payload

    def _response_from_upstream(
        self,
        payload: dict[str, Any],
        public_model: str,
    ) -> ChatCompletionResponse:
        text, finish = _candidate_text(payload)
        if text is None and finish is None and _blocked(payload):
            finish = "content_filter"
            text = ""
        return completion_response(
            completion_id=new_completion_id(),
            created=unix_timestamp(),
            model=public_model,
            content=text or "",
            finish_reason=finish or "stop",
            usage=usage_from_gemini(_dict(payload.get("usageMetadata"))),
        )


def _convert_messages(messages: list[ChatMessage]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        if message.role in {"system", "developer"}:
            text = message_text(message)
            if text:
                system_parts.append(text)
            continue
        if message.role not in {"user", "assistant"}:
            raise InvalidRequestError(
                f"Role '{message.role}' is not supported for gemini",
                param="messages",
            )
        role = "model" if message.role == "assistant" else "user"
        parts = gemini_parts(message)
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})
    if not contents:
        raise InvalidRequestError("At least one user message is required", param="messages")
    if contents[0]["role"] != "user":
        contents.insert(0, {"role": "user", "parts": [{"text": _CONTINUATION}]})
    system = "\n\n".join(system_parts) or None
    return system, contents


def _candidate_text(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None, "content_filter" if _blocked(payload) else None
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return None, None
    reason = candidate.get("finishReason")
    finish = _FINISH_REASONS.get(reason, "stop") if isinstance(reason, str) else None
    content = candidate.get("content")
    texts: list[str] = []
    if isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
    text = "".join(texts) if texts else None
    return text, finish


def _blocked(payload: dict[str, Any]) -> bool:
    feedback = payload.get("promptFeedback")
    return isinstance(feedback, dict) and bool(feedback.get("blockReason"))


def _dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    return None
