"""Per-provider circuit breaker stored in Redis."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog

log = structlog.get_logger("llmgate")


class BreakerRedis(Protocol):
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

    def incr(self, name: str) -> Awaitable[Any]: ...


class CircuitBreaker:
    """Skip a provider after consecutive transient failures.

    The open flag and the half-open probe store timestamps. Callers compare
    them to the recovery window, so a dead probe cannot block the provider
    forever and tests can move the clock.
    """

    def __init__(
        self,
        redis: BreakerRedis,
        *,
        failure_threshold: int,
        recovery_seconds: float,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.redis = redis
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.now = time.time if now is None else now

    async def allow(self, provider: str) -> bool:
        try:
            now = self.now()
            opened = _float(await self.redis.get(self._open_key(provider)))
            if opened is not None and now < opened + self.recovery_seconds:
                return False
            failures = _int(await self.redis.get(self._fail_key(provider)))
            if failures < self.failure_threshold and opened is None:
                return True
            return await self._claim_probe(provider, now)
        except Exception:
            log.warning("circuit.unavailable", provider=provider)
            return True

    async def record_success(self, provider: str) -> None:
        try:
            await self.redis.delete(
                self._fail_key(provider),
                self._open_key(provider),
                self._probe_key(provider),
            )
        except Exception:
            log.warning("circuit.unavailable", provider=provider)

    async def record_failure(self, provider: str) -> None:
        try:
            count = int(await self.redis.incr(self._fail_key(provider)))
            if count < self.failure_threshold:
                return
            now = self.now()
            opened = _float(await self.redis.get(self._open_key(provider)))
            already_open = opened is not None and now < opened + self.recovery_seconds
            await self.redis.set(self._open_key(provider), f"{now:.6f}")
            await self.redis.delete(self._probe_key(provider))
            if not already_open:
                log.warning("circuit.open", provider=provider, failures=count)
        except Exception:
            log.warning("circuit.unavailable", provider=provider)

    async def _claim_probe(self, provider: str, now: float) -> bool:
        probe_key = self._probe_key(provider)
        acquired = await self.redis.set(probe_key, f"{now:.6f}", nx=True)
        if acquired:
            return True
        existing = _float(await self.redis.get(probe_key))
        if existing is None or now >= existing + self.recovery_seconds:
            await self.redis.set(probe_key, f"{now:.6f}")
            return True
        return False

    def _fail_key(self, provider: str) -> str:
        return f"llmgate:cb:fail:{provider}"

    def _open_key(self, provider: str) -> str:
        return f"llmgate:cb:open:{provider}"

    def _probe_key(self, provider: str) -> str:
        return f"llmgate:cb:probe:{provider}"


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _float(value: object) -> float | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: object) -> int:
    text = _text(value)
    if text is None:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0
