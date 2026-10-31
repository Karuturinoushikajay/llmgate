"""Objects created at startup and shared by request handlers."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from llmgate.config import Settings
from llmgate.core.pipeline import ChatPipeline
from llmgate.core.ratelimit import TokenBucket
from llmgate.core.registry import ModelRegistry
from llmgate.providers.base import ChatProvider


class RedisClient(Protocol):
    """Redis commands the gateway uses. The bucket and breaker share this client."""

    def ping(self, **kwargs: Any) -> Awaitable[bool]: ...

    def aclose(self, *args: Any, **kwargs: Any) -> Awaitable[None]: ...

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

    def execute_command(self, *args: Any, **kwargs: Any) -> Awaitable[Any]: ...


@dataclass
class GatewayState:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: RedisClient
    http: httpx.AsyncClient
    registry: ModelRegistry
    providers: dict[str, ChatProvider]
    pipeline: ChatPipeline
    limiter: TokenBucket
