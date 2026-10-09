"""Adapter interface implemented by every provider."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Protocol

import httpx

from llmgate.core.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
)


class ChatProvider(Protocol):
    name: str

    async def complete(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        *,
        upstream_timeout: httpx.Timeout | None = None,
    ) -> ChatCompletionResponse: ...

    def stream(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        *,
        upstream_timeout: httpx.Timeout | None = None,
    ) -> AsyncGenerator[ChatCompletionChunk, None]: ...
