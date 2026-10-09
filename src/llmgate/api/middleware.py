"""Request ids and access logs.

This is raw ASGI middleware so streaming responses are not buffered. The
``await self.app(...)`` call returns only after the body has been sent, which
means the access log includes stream duration and the token counts the handler
stored in context vars.
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from llmgate.core.context import (
    api_key_id_var,
    error_var,
    model_var,
    provider_var,
    request_id_var,
    stream_var,
    usage_var,
)
from llmgate.logging import log_access


class AccessLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _header(scope, b"x-request-id") or str(uuid.uuid4())
        started = time.perf_counter()
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        tokens = (
            request_id_var.set(request_id),
            usage_var.set(None),
            model_var.set(None),
            provider_var.set(None),
            stream_var.set(False),
            api_key_id_var.set(None),
            error_var.set(None),
        )
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("utf-8")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            state = scope.get("state")
            log_access(
                method=str(scope.get("method", "")),
                path=str(scope.get("path", "")),
                status_code=status_code,
                started=started,
                scope_state=state if isinstance(state, dict) else None,
            )
            request_id_var.reset(tokens[0])
            usage_var.reset(tokens[1])
            model_var.reset(tokens[2])
            provider_var.reset(tokens[3])
            stream_var.reset(tokens[4])
            api_key_id_var.reset(tokens[5])
            error_var.reset(tokens[6])
            structlog.contextvars.clear_contextvars()


def _header(scope: Scope, name: bytes) -> str | None:
    raw_headers = scope.get("headers", [])
    if not isinstance(raw_headers, list):
        return None
    for item in raw_headers:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        key, value = item
        if isinstance(key, bytes) and key.lower() == name and isinstance(value, bytes):
            return value.decode("utf-8")
    return None
