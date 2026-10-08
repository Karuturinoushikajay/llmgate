"""The single seam every chat request passes through.

Milestone 1 resolves the model and returns the provider adapter. Later
milestones wrap this class (retries, fallback, circuit breakers, semantic
cache, routing, guardrails) without changing the HTTP handlers.
"""

from __future__ import annotations

from typing import Any

from llmgate.core.errors import InvalidRequestError
from llmgate.core.registry import ModelRegistry, ResolvedModel
from llmgate.core.schemas import ChatCompletionRequest
from llmgate.providers.base import ChatProvider


class ChatPipeline:
    def __init__(self, registry: ModelRegistry, providers: dict[str, ChatProvider]) -> None:
        self.registry = registry
        self.providers = providers

    def resolve(self, request: ChatCompletionRequest) -> tuple[ResolvedModel, ChatProvider]:
        if request.n not in (None, 1):
            raise InvalidRequestError("Only n=1 is supported", param="n")
        resolved = self.registry.resolve(request.model)
        self._reject_unsupported(request, resolved.provider)
        try:
            provider = self.providers[resolved.provider]
        except KeyError as exc:
            raise InvalidRequestError(
                f"Provider '{resolved.provider}' is not available",
                param="model",
            ) from exc
        return resolved, provider

    def _reject_unsupported(self, request: ChatCompletionRequest, provider: str) -> None:
        if provider == "openai":
            return
        if request.tools:
            raise InvalidRequestError(
                "Tool calling is not supported for this provider yet",
                param="tools",
            )
        if request.tool_choice is not None:
            raise InvalidRequestError(
                "tool_choice is not supported for this provider yet",
                param="tool_choice",
            )
        response_format: dict[str, Any] | None = request.response_format
        if response_format is not None and response_format.get("type") not in (None, "text"):
            raise InvalidRequestError(
                "response_format is not supported for this provider yet",
                param="response_format",
            )
        for message in request.messages:
            if message.role == "tool" or message.tool_calls:
                raise InvalidRequestError(
                    "Tool messages are not supported for this provider yet",
                    param="messages",
                )
