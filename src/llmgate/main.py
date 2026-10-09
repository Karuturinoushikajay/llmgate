"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from starlette.exceptions import HTTPException as StarletteHTTPException

from llmgate import __version__
from llmgate.api.middleware import AccessLogMiddleware
from llmgate.api.routes.admin import router as admin_router
from llmgate.api.routes.chat import router as chat_router
from llmgate.api.routes.health import router as health_router
from llmgate.api.routes.models import router as models_router
from llmgate.api.state import GatewayState, RedisClient
from llmgate.config import Settings, get_settings, resolve_models_path
from llmgate.core.breaker import CircuitBreaker
from llmgate.core.errors import GatewayError
from llmgate.core.limiter import TokenBucketLimiter
from llmgate.core.pipeline import ChatPipeline
from llmgate.core.registry import ModelRegistry
from llmgate.core.retry import RetryPolicy
from llmgate.core.schemas import unix_timestamp
from llmgate.logging import configure_logging
from llmgate.providers import build_providers
from llmgate.storage.db import create_engine, create_session_factory, upgrade_database

log = structlog.get_logger("llmgate")


def create_app(
    settings: Settings | None = None,
    *,
    redis_client: RedisClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=settings.request_timeout_seconds,
                write=30.0,
                pool=10.0,
            ),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
        redis: RedisClient
        if redis_client is not None:
            redis = redis_client
        else:
            redis = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
            )
        try:
            await upgrade_database(engine, settings.alembic_ini)
            try:
                await redis.ping()
                log.info("redis.connected", url=_redact_url(settings.redis_url))
            except Exception:
                log.warning("redis.unavailable", url=_redact_url(settings.redis_url))
            registry = ModelRegistry.load(
                resolve_models_path(settings.models_path),
                loaded_at=unix_timestamp(),
            )
            providers = build_providers(settings, http)
            pipeline = ChatPipeline(
                registry,
                providers,
                retry_policy=RetryPolicy(
                    max_attempts=settings.retry_max_attempts,
                    base_delay_seconds=settings.retry_base_delay_seconds,
                    max_delay_seconds=settings.retry_max_delay_seconds,
                    retry_after_cap_seconds=settings.retry_after_cap_seconds,
                ),
                breaker=CircuitBreaker(
                    redis,
                    failure_threshold=settings.breaker_failure_threshold,
                    cooldown_seconds=settings.breaker_cooldown_seconds,
                    half_open_max=settings.breaker_half_open_max,
                ),
                limiter=TokenBucketLimiter(redis),
                output_token_reservation=settings.output_token_reservation,
            )
            app.state.gateway = GatewayState(
                settings=settings,
                engine=engine,
                session_factory=create_session_factory(engine),
                redis=redis,
                http=http,
                registry=registry,
                providers=providers,
                pipeline=pipeline,
            )
            log.info("llmgate.started", version=__version__, models=len(registry.list_models()))
            if settings.using_default_secrets:
                log.warning(
                    "llmgate.insecure_defaults",
                    hint="Set LLMGATE_MASTER_KEY and LLMGATE_KEY_PEPPER",
                )
            yield
        finally:
            app.state.gateway = None
            await http.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="LLMGate",
        version=__version__,
        summary="OpenAI-compatible gateway for multiple LLM providers",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.gateway = None
    app.add_middleware(AccessLogMiddleware)
    app.include_router(health_router)
    app.include_router(models_router)
    app.include_router(chat_router)
    app.include_router(admin_router)
    _register_exception_handlers(app)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": "LLMGate", "version": __version__, "docs": "/docs"}

    return app


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def gateway_error_handler(_request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_body(),
            headers=exc.response_headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        message = "Invalid request"
        param: str | None = None
        if errors:
            first = errors[0]
            message = str(first.get("msg", message))
            loc = [str(part) for part in first.get("loc", []) if part != "body"]
            param = ".".join(loc) or None
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": message,
                    "type": "invalid_request_error",
                    "param": param,
                    "code": "invalid_request",
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        error_type = "invalid_request_error" if exc.status_code < 500 else "server_error"
        code = "not_found" if exc.status_code == 404 else None
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": message,
                    "type": error_type,
                    "param": None,
                    "code": code,
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # GatewayError is registered separately; this is the last-resort shape.
        log.exception("request.unhandled", path=request.url.path, error=exc.__class__.__name__)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "Internal server error",
                    "type": "server_error",
                    "param": None,
                    "code": "internal_error",
                }
            },
        )


def _redact_url(url: str) -> str:
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://***@{host}"


app = create_app()
