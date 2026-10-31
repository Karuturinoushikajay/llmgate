"""The single seam every chat request passes through.

The pipeline resolves the model, consults the semantic cache, retries
transient failures, follows the registry fallback list, and skips providers
whose circuit is open. The HTTP handlers stay responsible for auth, SSE
framing, and the access log.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from llmgate.config import Settings
from llmgate.core.breaker import CircuitBreaker
from llmgate.core.cache import SemanticCache
from llmgate.core.context import cache_var, model_var, provider_var
from llmgate.core.embeddings import Embedder, embedding_input
from llmgate.core.errors import (
    GatewayError,
    InvalidRequestError,
    ProviderError,
    ProviderNotConfiguredError,
    RateLimitError,
)
from llmgate.core.registry import ModelRegistry, ResolvedModel
from llmgate.core.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Usage,
    completion_chunk,
    completion_response,
)
from llmgate.providers.base import ChatProvider

log = structlog.get_logger("llmgate")

Sleep = Callable[[float], Awaitable[None]]


@dataclass
class _Prepared:
    response: ChatCompletionResponse | None = None
    vector: list[float] | None = None


class ChatPipeline:
    def __init__(
        self,
        registry: ModelRegistry,
        providers: dict[str, ChatProvider],
        settings: Settings,
        breaker: CircuitBreaker,
        *,
        cache: SemanticCache | None = None,
        embedder: Embedder | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self.registry = registry
        self.providers = providers
        self.settings = settings
        self.breaker = breaker
        self.cache = cache
        self.embedder = embedder
        self.sleep: Sleep = _default_sleep if sleep is None else sleep

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        prepared = await self._prepare(request)
        if prepared.response is not None:
            return prepared.response
        result = await self._complete(request)
        await self._store(request, prepared.vector, result)
        return result

    async def _complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        last_error: GatewayError | None = None
        candidates = self._candidates(request)
        for index, candidate in enumerate(candidates):
            self._bind(candidate)
            if not await self.breaker.allow(candidate.provider):
                self._log_fallback(candidates, index, "circuit_open")
                continue
            provider = self._provider(candidate)
            for attempt in range(self.settings.retry_max_attempts):
                try:
                    result = await provider.complete(request, candidate.upstream_model)
                except Exception as exc:
                    mapped = _map_error(candidate.provider, exc)
                    action = _disposition(mapped)
                    if action == "raise":
                        raise mapped from None
                    if action == "retry":
                        await self.breaker.record_failure(candidate.provider)
                        if attempt + 1 < self.settings.retry_max_attempts:
                            await self._pause(candidate.provider, attempt)
                            continue
                    last_error = mapped
                    break
                else:
                    await self.breaker.record_success(candidate.provider)
                    return result
            self._log_fallback(candidates, index, "error")
        if last_error is not None:
            raise last_error
        raise ProviderError(
            "No healthy provider available for this model",
            status_code=503,
        )

    async def stream(
        self,
        request: ChatCompletionRequest,
    ) -> AsyncGenerator[ChatCompletionChunk, None]:
        prepared = await self._prepare(request)
        if prepared.response is not None:
            for chunk in _replay(prepared.response):
                yield chunk
            return
        opened = await self._open_stream(request)
        if opened is None:
            return
        iterator, first = opened
        collected = [first]
        try:
            yield first
            async for chunk in iterator:
                collected.append(chunk)
                yield chunk
        finally:
            await iterator.aclose()
        assembled = _response_from_chunks(request.model, collected)
        if assembled is not None:
            await self._store(request, prepared.vector, assembled)

    async def _open_stream(
        self,
        request: ChatCompletionRequest,
    ) -> tuple[AsyncGenerator[ChatCompletionChunk, None], ChatCompletionChunk] | None:
        last_error: GatewayError | None = None
        candidates = self._candidates(request)
        for index, candidate in enumerate(candidates):
            self._bind(candidate)
            if not await self.breaker.allow(candidate.provider):
                self._log_fallback(candidates, index, "circuit_open")
                continue
            provider = self._provider(candidate)
            for attempt in range(self.settings.retry_max_attempts):
                iterator = provider.stream(request, candidate.upstream_model)
                try:
                    first = await anext(iterator)
                except StopAsyncIteration:
                    await iterator.aclose()
                    await self.breaker.record_success(candidate.provider)
                    return None
                except Exception as exc:
                    await iterator.aclose()
                    mapped = _map_error(candidate.provider, exc)
                    action = _disposition(mapped)
                    if action == "raise":
                        raise mapped from None
                    if action == "retry":
                        await self.breaker.record_failure(candidate.provider)
                        if attempt + 1 < self.settings.retry_max_attempts:
                            await self._pause(candidate.provider, attempt)
                            continue
                    last_error = mapped
                    break
                else:
                    await self.breaker.record_success(candidate.provider)
                    return iterator, first
            self._log_fallback(candidates, index, "error")
        if last_error is not None:
            raise last_error
        raise ProviderError(
            "No healthy provider available for this model",
            status_code=503,
        )

    async def _prepare(self, request: ChatCompletionRequest) -> _Prepared:
        if not self._eligible(request) or self.cache is None or self.embedder is None:
            cache_var.set("bypass")
            return _Prepared()
        text = embedding_input(request).strip()
        if not text:
            cache_var.set("bypass")
            return _Prepared()
        try:
            vector = await self.embedder.embed(text)
            hit = await self.cache.lookup(model=request.model, vector=vector)
        except Exception as exc:
            log.warning("cache.bypass", error=type(exc).__name__)
            cache_var.set("bypass")
            return _Prepared()
        if hit is not None:
            cache_var.set("hit")
            model_var.set(request.model)
            provider_var.set("cache")
            log.info("cache.hit", model=request.model, score=round(hit.score, 4))
            return _Prepared(response=hit.response)
        cache_var.set("miss")
        return _Prepared(vector=vector)

    async def _store(
        self,
        request: ChatCompletionRequest,
        vector: list[float] | None,
        response: ChatCompletionResponse,
    ) -> None:
        if vector is None or self.cache is None or not response.choices:
            return
        try:
            await self.cache.store(model=request.model, vector=vector, response=response)
        except Exception as exc:
            log.warning("cache.store_failed", error=type(exc).__name__)

    def _eligible(self, request: ChatCompletionRequest) -> bool:
        if not self.settings.cache_enabled:
            return False
        if request.temperature is None:
            return False
        if request.temperature > self.settings.cache_max_temperature:
            return False
        if request.n not in (None, 1):
            return False
        if request.tools or request.tool_choice is not None:
            return False
        return not any(message.role == "tool" or message.tool_calls for message in request.messages)

    def _candidates(self, request: ChatCompletionRequest) -> list[ResolvedModel]:
        if request.n not in (None, 1):
            raise InvalidRequestError("Only n=1 is supported", param="n")
        primary = self.registry.resolve(request.model)
        self._reject_unsupported(request, primary.provider)
        if "/" in request.model:
            return [primary]
        return self._walk(primary.public_name, request, set())

    def _walk(
        self,
        name: str,
        request: ChatCompletionRequest,
        seen: set[str],
    ) -> list[ResolvedModel]:
        if name in seen:
            return []
        seen.add(name)
        resolved = self.registry.resolve(name)
        chain: list[ResolvedModel] = []
        if not _incompatible(request, resolved.provider):
            chain.append(resolved)
        entry = self.registry.get(resolved.public_name)
        if entry is None:
            return chain
        for nxt in entry.fallbacks:
            chain.extend(self._walk(nxt, request, seen))
        return chain

    def _provider(self, candidate: ResolvedModel) -> ChatProvider:
        try:
            return self.providers[candidate.provider]
        except KeyError as exc:
            raise InvalidRequestError(
                f"Provider '{candidate.provider}' is not available",
                param="model",
            ) from exc

    def _bind(self, candidate: ResolvedModel) -> None:
        model_var.set(candidate.public_name)
        provider_var.set(candidate.provider)

    async def _pause(self, provider: str, attempt: int) -> None:
        delay = _backoff(
            attempt,
            base=self.settings.retry_base_seconds,
            cap=self.settings.retry_max_seconds,
        )
        log.info(
            "retry.scheduled",
            provider=provider,
            attempt=attempt + 1,
            delay_seconds=round(delay, 3),
        )
        await self.sleep(delay)

    def _log_fallback(self, candidates: list[ResolvedModel], index: int, reason: str) -> None:
        if index + 1 >= len(candidates):
            return
        log.info(
            "provider.fallback",
            from_model=candidates[index].public_name,
            to_model=candidates[index + 1].public_name,
            reason=reason,
        )

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


def _incompatible(request: ChatCompletionRequest, provider: str) -> bool:
    if provider == "openai":
        return False
    if request.tools or request.tool_choice is not None:
        return True
    response_format = request.response_format
    if response_format is not None and response_format.get("type") not in (None, "text"):
        return True
    return any(message.role == "tool" or message.tool_calls for message in request.messages)


def _disposition(exc: GatewayError) -> str:
    if isinstance(exc, ProviderNotConfiguredError):
        return "fallback"
    if isinstance(exc, RateLimitError):
        return "retry"
    if isinstance(exc, ProviderError) and exc.status_code in (502, 504):
        return "retry"
    return "raise"


def _map_error(provider_name: str, exc: Exception) -> GatewayError:
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(f"Timed out waiting for {provider_name}", status_code=504)
    if isinstance(exc, httpx.HTTPError):
        return ProviderError(f"Failed to reach {provider_name}")
    raise exc


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _replay(response: ChatCompletionResponse) -> list[ChatCompletionChunk]:
    choice = response.choices[0]
    return [
        completion_chunk(
            completion_id=response.id,
            created=response.created,
            model=response.model,
            content=choice.message.content,
            role="assistant",
            finish_reason=choice.finish_reason,
            usage=response.usage,
            tool_calls=choice.message.tool_calls,
        )
    ]


def _response_from_chunks(
    model: str,
    chunks: list[ChatCompletionChunk],
) -> ChatCompletionResponse | None:
    if not chunks:
        return None
    texts: list[str] = []
    usage: Usage | None = None
    finish = "stop"
    completion_id = chunks[0].id
    created = chunks[0].created
    tool_calls: list[dict[str, Any]] | None = None
    for chunk in chunks:
        if chunk.usage is not None:
            usage = chunk.usage
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if choice.delta.content:
            texts.append(choice.delta.content)
        if choice.delta.tool_calls:
            tool_calls = choice.delta.tool_calls
        if choice.finish_reason:
            finish = choice.finish_reason
    if not texts and tool_calls is None:
        return None
    return completion_response(
        completion_id=completion_id,
        created=created,
        model=model,
        content="".join(texts) if texts else None,
        finish_reason=finish,
        usage=usage if usage is not None else Usage(),
        tool_calls=tool_calls,
    )


def _backoff(attempt: int, *, base: float, cap: float) -> float:
    window = min(cap, base * (2**attempt))
    if window <= 0:
        return 0
    return random.uniform(0, window)
