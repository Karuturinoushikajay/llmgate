"""The Redis operations the gateway uses. Implementations: redis.asyncio and fakeredis."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol


class RedisClient(Protocol):
    def ping(self, **kwargs: Any) -> Awaitable[bool]: ...

    def aclose(self, *args: Any, **kwargs: Any) -> Awaitable[None]: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Awaitable[Any]: ...
