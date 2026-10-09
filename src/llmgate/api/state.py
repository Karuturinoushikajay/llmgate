"""Objects created at startup and shared by request handlers."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from llmgate.config import Settings
from llmgate.core.pipeline import ChatPipeline
from llmgate.core.registry import ModelRegistry
from llmgate.providers.base import ChatProvider


class RedisClient(Protocol):
    """The slice of Redis milestone 1 uses. Rate limiting will widen this."""

    def ping(self, **kwargs: Any) -> Awaitable[bool]: ...

    def aclose(self, *args: Any, **kwargs: Any) -> Awaitable[None]: ...


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
