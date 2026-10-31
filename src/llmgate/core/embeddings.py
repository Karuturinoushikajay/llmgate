"""OpenAI embeddings for the semantic cache."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol

import httpx

from llmgate.core.schemas import ChatCompletionRequest, ContentPart


class CacheBypass(Exception):
    """The cache cannot run. The provider call should proceed."""


class Embedder(Protocol):
    def embed(self, text: str) -> Awaitable[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, api_key: str, base_url: str, model: str, http: httpx.AsyncClient) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.http = http

    async def embed(self, text: str) -> list[float]:
        if not self.api_key:
            raise CacheBypass("embeddings are not configured")
        try:
            response = await self.http.post(
                f"{self.base_url}/embeddings",
                json={"model": self.model, "input": text},
                headers={"authorization": f"Bearer {self.api_key}"},
            )
        except httpx.HTTPError as exc:
            raise CacheBypass("embedding request failed") from exc
        if response.status_code >= 400:
            raise CacheBypass("embedding request failed")
        return _vector(response.json())


def embedding_input(request: ChatCompletionRequest) -> str:
    """Role and text of each message. Images and raw JSON are left out."""
    lines: list[str] = []
    for message in request.messages:
        lines.append(f"{message.role}: {_content_text(message.content)}")
    return "\n".join(lines)


def _content_text(content: str | list[ContentPart] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return " ".join(part.text for part in content if part.text)


def _vector(payload: object) -> list[float]:
    if not isinstance(payload, dict):
        raise CacheBypass("embedding response was empty")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise CacheBypass("embedding response was empty")
    first = data[0]
    if not isinstance(first, dict):
        raise CacheBypass("embedding response was empty")
    embedding = first.get("embedding")
    if not isinstance(embedding, list) or not embedding:
        raise CacheBypass("embedding response was empty")
    try:
        return [float(item) for item in embedding]
    except (TypeError, ValueError) as exc:
        raise CacheBypass("embedding response was empty") from exc
