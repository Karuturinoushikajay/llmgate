"""Per-key token bucket stored in Redis."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog

from llmgate.core.errors import GatewayError, RateLimitError, ServiceUnavailableError

log = structlog.get_logger("llmgate")

_LOCK_ATTEMPTS = 8
_LOCK_TTL_MS = 500


class BucketRedis(Protocol):
    def get(self, name: str) -> Awaitable[Any]: ...

    def set(
        self,
        name: str,
        value: str,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> Awaitable[Any]: ...

    def delete(self, *names: str) -> Awaitable[Any]: ...


class TokenBucket:
    """One bucket per gateway key.

    A short ``SET NX`` lock serializes the read-modify-write. Redis errors
    fail closed so a gateway that cannot enforce quota does not spend
    provider money.
    """

    def __init__(
        self,
        redis: BucketRedis,
        *,
        burst: float,
        refill_per_second: float,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.redis = redis
        self.burst = burst
        self.refill_per_second = refill_per_second
        self.now = time.monotonic if now is None else now

    async def consume(self, key_id: str) -> None:
        lock_name = f"llmgate:rl:lock:{key_id}"
        state_name = f"llmgate:rl:{key_id}"
        try:
            if not await self._lock(lock_name):
                raise ServiceUnavailableError(
                    "Rate limiter is unavailable",
                    code="rate_limiter_unavailable",
                )
            try:
                retry_after = await self._take(state_name)
            finally:
                await self.redis.delete(lock_name)
        except GatewayError:
            raise
        except Exception as exc:
            log.warning("ratelimit.unavailable", error=type(exc).__name__)
            raise ServiceUnavailableError(
                "Rate limiter is unavailable",
                code="rate_limiter_unavailable",
            ) from exc
        if retry_after is not None:
            raise RateLimitError("Rate limit exceeded", retry_after=retry_after)

    async def _lock(self, name: str) -> bool:
        for _ in range(_LOCK_ATTEMPTS):
            acquired = await self.redis.set(name, "1", nx=True, px=_LOCK_TTL_MS)
            if acquired:
                return True
            await asyncio.sleep(0.01)
        return False

    async def _take(self, state_name: str) -> int | None:
        now = self.now()
        raw = _text(await self.redis.get(state_name))
        tokens = float(self.burst)
        if raw is not None:
            tokens = _refilled(raw, now, self.burst, self.refill_per_second)
        if tokens >= 1:
            await self.redis.set(state_name, f"{tokens - 1:.6f},{now:.6f}")
            return None
        deficit = 1 - tokens
        retry_after = max(1, math.ceil(deficit / self.refill_per_second))
        await self.redis.set(state_name, f"{tokens:.6f},{now:.6f}")
        return retry_after


def _refilled(raw: str, now: float, burst: float, refill_per_second: float) -> float:
    try:
        tokens_text, timestamp_text = raw.split(",", 1)
        tokens = float(tokens_text)
        timestamp = float(timestamp_text)
    except ValueError:
        return burst
    elapsed = max(0.0, now - timestamp)
    return min(burst, tokens + elapsed * refill_per_second)


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode()
    return str(value)
