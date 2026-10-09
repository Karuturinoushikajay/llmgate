"""FastAPI dependencies for gateway state and authentication."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from llmgate.api.state import GatewayState
from llmgate.core.context import api_key_id_var
from llmgate.core.errors import AuthenticationError
from llmgate.storage.db import session_scope
from llmgate.storage.keys import get_active_key_by_token

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class ApiKeyContext:
    id: UUID
    name: str
    key_prefix: str
    requests_per_minute: int
    tokens_per_minute: int


def get_gateway(request: Request) -> GatewayState:
    gateway: GatewayState | None = getattr(request.app.state, "gateway", None)
    if gateway is None:
        raise RuntimeError("LLMGate is not initialized")
    return gateway


def _secrets_equal(left: str, right: str) -> bool:
    # Hash first so compare_digest does not leak the length of the master key.
    left_digest = hashlib.sha256(left.encode("utf-8")).digest()
    right_digest = hashlib.sha256(right.encode("utf-8")).digest()
    return hmac.compare_digest(left_digest, right_digest)


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ApiKeyContext:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise AuthenticationError("Missing API key. Provide Authorization: Bearer <key>.")
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        row = await get_active_key_by_token(
            session,
            credentials.credentials,
            gateway.settings.key_pepper,
        )
        if row is None:
            raise AuthenticationError()
        api_key_id_var.set(str(row.id))
        settings = gateway.settings
        return ApiKeyContext(
            id=row.id,
            name=row.name,
            key_prefix=row.key_prefix,
            requests_per_minute=(
                settings.default_requests_per_minute
                if row.requests_per_minute is None
                else row.requests_per_minute
            ),
            tokens_per_minute=(
                settings.default_tokens_per_minute
                if row.tokens_per_minute is None
                else row.tokens_per_minute
            ),
        )


async def require_master_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    gateway = get_gateway(request)
    presented = "" if credentials is None else credentials.credentials
    if credentials is None or credentials.scheme.lower() != "bearer":
        presented = ""
    if not _secrets_equal(presented, gateway.settings.master_key):
        raise AuthenticationError("Invalid master key", code="invalid_master_key")
