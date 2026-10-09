"""The single seam every chat request passes through.

Retries, provider fallback, and the circuit breaker wrap the provider adapters
here. The HTTP handlers stay responsible for auth, SSE framing, and status codes.
Later milestones (cache, routing, guardrails) wrap this class the same way.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
import structlog

from llmgate.core.breaker import CircuitBreaker
from llmgate.core.errors import (
    CircuitOpenError,
    GatewayError,
    InvalidRequestError,
    ProviderError,
)
from llmgate.core.limiter import LimitDecision, TokenBucketLimiter
from llmgate.core.registry import ModelRegistry, ModelTarget, ResolvedModel
from llmgate.core.retry import (
    RetryPolicy,
    backoff_delay,
    counts_toward_breaker,
    map_provider_exception,
    should_fallback,
    should_retry,
)
from llmgate.core.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Usage,
)
from llmgate.core.tokens import estimate_tokens
from llmgate.providers.base import ChatProvider

log = structlog.get_logger("llmgate.pipeline")

T = TypeVar("T")


@dataclass(frozen=True)
class KeyLimits:
    key_id: str
    requests_per_minute: int
    tokens_per_minute: int


@dataclass(frozen=True)
class ServedCompletion:
    response: ChatCompletionResponse
    provider: str
    upstream_model: str
    headers: dict[str, str]


@dataclass
class OpenedStream:
    provider: str
    upstream_model: str
    first: ChatCompletionChunk | None
    chunks: AsyncGenerator[ChatCompletionChunk, None]
    decision: LimitDecision
    reserved_tokens: int
    limits: KeyLimits

    async def aclose(self) -> None:
        await self.chunks.aclose()

    @property
    def headers(self) -> dict[str, str]:
        headers = self.decision.headers()
        headers["x-llmgate-provider"] = self.provider
        headers["x-llmgate-model"] = self.upstream_model
        return headers


class ChatPipeline:
    def __init__(
        self,
        registry: ModelRegistry,
        providers: dict[str, ChatProvider],
        *,
        retry_policy: RetryPolicy,
        breaker: CircuitBreaker,
        limiter: TokenBucketLimiter,
        output_token_reservation: int = 256,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        clock_ms: Callable[[], int] | None = None,
        random_unit: Callable[[], float] | None = None,
    ) -> None:
        self.registry = registry
        self.providers = providers
        self.retry_policy = retry_policy
        self.breaker = breaker
        self.limiter = limiter
        self.output_token_reservation = output_token_reservation
        self._sleep = sleeper or _sleep
        self._now_ms = clock_ms or _now_ms
        self._random_unit = random_unit or random.random

    def resolve(self, request: ChatCompletionRequest) -> tuple[ResolvedModel, ChatProvider]:
        """Primary target only. Kept for callers that just need the mapping."""
        self._reject_n(request)
        resolved = self.registry.resolve(request.model)
        self._reject_unsupported(request, resolved.provider)
        return resolved, self._provider(resolved.provider)

    async def complete(
        self,
        request: ChatCompletionRequest,
        *,
        limits: KeyLimits,
        upstream_timeout: httpx.Timeout,
    ) -> ServedCompletion:
        self._reject_n(request)
        resolved = self.registry.resolve(request.model)
        decision, reserved = await self._reserve(request, limits)

        async def operation(target: ModelTarget) -> ChatCompletionResponse:
            provider = self._provider(target.provider)
            return await provider.complete(
                request,
                target.upstream_model,
                upstream_timeout=upstream_timeout,
            )

        try:
            response, target = await self._execute(request, resolved, operation)
        except Exception:
            await self._refund(limits, reserved)
            raise
        decision = await self._reconcile(
            limits,
            reserved,
            None if response.usage is None else response.usage.total_tokens,
            decision,
        )
        return ServedCompletion(
            response=response,
            provider=target.provider,
            upstream_model=target.upstream_model,
            headers=_headers(target, decision),
        )

    async def open_stream(
        self,
        request: ChatCompletionRequest,
        *,
        limits: KeyLimits,
        upstream_timeout: httpx.Timeout,
    ) -> OpenedStream:
        """Open a stream, retrying only until the first chunk is available."""
        self._reject_n(request)
        resolved = self.registry.resolve(request.model)
        decision, reserved = await self._reserve(request, limits)

        async def operation(
            target: ModelTarget,
        ) -> tuple[AsyncGenerator[ChatCompletionChunk, None], ChatCompletionChunk | None]:
            provider = self._provider(target.provider)
            iterator = provider.stream(
                request,
                target.upstream_model,
                upstream_timeout=upstream_timeout,
            )
            try:
                first = await anext(iterator)
            except StopAsyncIteration:
                return iterator, None
            except BaseException:
                await iterator.aclose()
                raise
            return iterator, first

        try:
            opened, target = await self._execute(request, resolved, operation)
        except Exception:
            await self._refund(limits, reserved)
            raise
        iterator, first = opened
        return OpenedStream(
            provider=target.provider,
            upstream_model=target.upstream_model,
            first=first,
            chunks=iterator,
            decision=decision,
            reserved_tokens=reserved,
            limits=limits,
        )

    async def settle_stream(self, opened: OpenedStream, usage: Usage | None) -> None:
        """Replace the token reservation with measured usage, when the provider reported it."""
        actual = None if usage is None else usage.total_tokens
        opened.decision = await self._reconcile(
            opened.limits,
            opened.reserved_tokens,
            actual,
            opened.decision,
        )

    async def _execute(
        self,
        request: ChatCompletionRequest,
        resolved: ResolvedModel,
        operation: Callable[[ModelTarget], Awaitable[T]],
    ) -> tuple[T, ModelTarget]:
        last_error: GatewayError | None = None
        for index, target in enumerate(resolved.targets):
            incompatible = self._compatibility_error(request, target.provider)
            if incompatible is not None:
                # The primary is what the caller asked for. A fallback that cannot
                # represent the request is skipped so the upstream error still wins.
                if index == 0:
                    raise incompatible
                continue
            try:
                result = await self._call_target(target, operation)
            except GatewayError as exc:
                last_error = exc
                if not should_fallback(exc):
                    raise
                log.info(
                    "upstream.fallback",
                    provider=target.provider,
                    model=target.upstream_model,
                    code=exc.code,
                    status_code=exc.status_code,
                )
                continue
            return result, target
        if last_error is None:
            raise ProviderError(
                "No provider could serve the request",
                status_code=503,
                code="no_provider",
                retryable=False,
            )
        raise last_error

    async def _call_target(
        self,
        target: ModelTarget,
        operation: Callable[[ModelTarget], Awaitable[T]],
    ) -> T:
        now = self._now_ms()
        if not await self.breaker.allow(target.provider, now_ms=now):
            raise CircuitOpenError(target.provider)

        async def attempt() -> T:
            return await operation(target)

        try:
            result = await self._with_retries(target.provider, attempt)
        except GatewayError as exc:
            if counts_toward_breaker(exc):
                await self.breaker.record_failure(target.provider, now_ms=self._now_ms())
            raise
        await self.breaker.record_success(target.provider, now_ms=self._now_ms())
        return result

    async def _with_retries(self, provider_name: str, operation: Callable[[], Awaitable[T]]) -> T:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await operation()
            except Exception as exc:
                mapped = map_provider_exception(provider_name, exc)
                if not should_retry(mapped, attempt=attempt, policy=self.retry_policy):
                    raise mapped from exc
                delay = backoff_delay(
                    attempt=attempt,
                    policy=self.retry_policy,
                    retry_after=mapped.retry_after,
                    random_unit=self._random_unit(),
                )
                log.info(
                    "upstream.retry",
                    provider=provider_name,
                    attempt=attempt,
                    delay_seconds=round(delay, 3),
                    code=mapped.code,
                )
                await self._sleep(delay)

    async def _reserve(
        self,
        request: ChatCompletionRequest,
        limits: KeyLimits,
    ) -> tuple[LimitDecision, int]:
        reserved = estimate_tokens(request, output_reservation=self.output_token_reservation)
        decision = await self.limiter.acquire(
            key_id=limits.key_id,
            requests_per_minute=limits.requests_per_minute,
            tokens_per_minute=limits.tokens_per_minute,
            token_cost=reserved,
            now_ms=self._now_ms(),
        )
        if not decision.allowed:
            raise decision.as_error()
        return decision, reserved

    async def _refund(self, limits: KeyLimits, reserved: int) -> None:
        if reserved <= 0:
            return
        try:
            await self.limiter.adjust(
                key_id=limits.key_id,
                tokens_per_minute=limits.tokens_per_minute,
                delta=-reserved,
                now_ms=self._now_ms(),
            )
        except GatewayError:
            log.warning("limiter.refund_failed", key_id=limits.key_id)

    async def _reconcile(
        self,
        limits: KeyLimits,
        reserved: int,
        actual: int | None,
        decision: LimitDecision,
    ) -> LimitDecision:
        if actual is None:
            return decision
        delta = actual - reserved
        if delta == 0:
            return decision
        try:
            remaining, reset_ms = await self.limiter.adjust(
                key_id=limits.key_id,
                tokens_per_minute=limits.tokens_per_minute,
                delta=delta,
                now_ms=self._now_ms(),
            )
        except GatewayError:
            log.warning("limiter.reconcile_failed", key_id=limits.key_id)
            return decision
        return decision.with_tokens(remaining, reset_ms)

    def _provider(self, name: str) -> ChatProvider:
        try:
            return self.providers[name]
        except KeyError as exc:
            raise InvalidRequestError(
                f"Provider '{name}' is not available",
                param="model",
            ) from exc

    def _reject_n(self, request: ChatCompletionRequest) -> None:
        if request.n not in (None, 1):
            raise InvalidRequestError("Only n=1 is supported", param="n")

    def _compatibility_error(
        self,
        request: ChatCompletionRequest,
        provider: str,
    ) -> InvalidRequestError | None:
        try:
            self._reject_unsupported(request, provider)
        except InvalidRequestError as exc:
            return exc
        return None

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


def _headers(target: ModelTarget, decision: LimitDecision) -> dict[str, str]:
    headers = decision.headers()
    headers["x-llmgate-provider"] = target.provider
    headers["x-llmgate-model"] = target.upstream_model
    return headers


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)
