"""Liveness and dependency checks."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from llmgate.api.deps import get_gateway
from llmgate.api.state import RedisClient

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    gateway = get_gateway(request)
    postgres = await _check_postgres(gateway.engine)
    redis_status = await _check_redis(gateway.redis)
    providers = {
        "openai": bool(gateway.settings.openai_api_key),
        "anthropic": bool(gateway.settings.anthropic_api_key),
        "gemini": bool(gateway.settings.gemini_api_key),
        "ollama": bool(gateway.settings.ollama_base_url),
    }
    healthy = postgres == "ok" and redis_status == "ok"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "postgres": postgres,
            "redis": redis_status,
            "providers": providers,
        },
    )


async def _check_postgres(engine: AsyncEngine) -> str:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return "error"
    return "ok"


async def _check_redis(redis: RedisClient) -> str:
    try:
        await redis.ping()
    except Exception:
        return "error"
    return "ok"
