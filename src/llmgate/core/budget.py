"""Monthly per-team token cap stored in Redis."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from llmgate.core.errors import RateLimitError, ServiceUnavailableError

log = structlog.get_logger("llmgate")

_TTL_SECONDS = 40 * 24 * 60 * 60


class BudgetRedis(Protocol):
    def get(self, name: str) -> Awaitable[Any]: ...

    def incrby(self, name: str, amount: int) -> Awaitable[Any]: ...

    def expire(self, name: str, seconds: int) -> Awaitable[Any]: ...


class TokenBudget:
    """One counter per team per UTC month.

    ``allow`` fails closed when Redis cannot be read. ``charge`` is called
    only after a provider call succeeds. A missing usage is the caller's
    problem to log, not this class's.
    """

    def __init__(self, redis: BudgetRedis, *, now: Callable[[], datetime] | None = None) -> None:
        self.redis = redis
        self.now: Callable[[], datetime] = _utc_now if now is None else now

    async def allow(self, team_id: str, cap: int) -> None:
        key = self._key(team_id)
        try:
            used = _int(await self.redis.get(key))
        except Exception as exc:
            log.warning("budget.unavailable", error=type(exc).__name__)
            raise ServiceUnavailableError(
                "Token budget is unavailable",
                code="budget_unavailable",
            ) from exc
        if used >= cap:
            raise RateLimitError(
                "Monthly token budget exceeded",
                retry_after=seconds_until_next_month(self.now()),
                code="budget_exceeded",
            )

    async def charge(self, team_id: str, tokens: int) -> None:
        if tokens <= 0:
            return
        key = self._key(team_id)
        await self.redis.incrby(key, tokens)
        await self.redis.expire(key, _TTL_SECONDS)

    def _key(self, team_id: str) -> str:
        return f"llmgate:budget:{team_id}:{self.now():%Y-%m}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def seconds_until_next_month(now: datetime) -> int:
    if now.month == 12:
        nxt = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    else:
        nxt = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return max(1, int((nxt - current).total_seconds()))


def _int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        value = value.decode()
    try:
        return int(str(value))
    except ValueError:
        return 0
