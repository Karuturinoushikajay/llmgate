"""Objects created at startup and shared by request handlers."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from llmgate.config import Settings
from llmgate.core.pipeline import ChatPipeline
from llmgate.core.redis_client import RedisClient
from llmgate.core.registry import ModelRegistry
from llmgate.providers.base import ChatProvider

__all__ = ["GatewayState", "RedisClient"]


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
