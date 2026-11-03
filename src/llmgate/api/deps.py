"""FastAPI dependencies for gateway state and authentication."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from llmgate.api.state import GatewayState
from llmgate.core.context import api_key_id_var, team_budget_var, team_id_var
from llmgate.core.errors import AuthenticationError, InvalidRequestError
from llmgate.storage.admin_users import get_active_admin_by_token
from llmgate.storage.db import session_scope
from llmgate.storage.keys import get_active_key_by_token
from llmgate.storage.models import Team

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class ApiKeyContext:
    id: UUID
    name: str
    key_prefix: str
    team_id: UUID | None


@dataclass(frozen=True)
class AdminContext:
    actor: str
    role: str
    is_master: bool


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
        team_id_var.set(str(row.team_id) if row.team_id is not None else None)
        if row.team_id is not None:
            team = await session.get(Team, row.team_id)
            if team is not None and team.monthly_token_budget is not None:
                team_budget_var.set(team.monthly_token_budget)
        return ApiKeyContext(
            id=row.id,
            name=row.name,
            key_prefix=row.key_prefix,
            team_id=row.team_id,
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


async def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AdminContext:
    """Master key or an active dashboard user token."""
    gateway = get_gateway(request)
    presented = "" if credentials is None else credentials.credentials
    if credentials is None or credentials.scheme.lower() != "bearer" or not presented:
        raise AuthenticationError("Invalid admin credentials", code="invalid_admin")
    if _secrets_equal(presented, gateway.settings.master_key):
        return AdminContext(actor="master", role="admin", is_master=True)
    async with session_scope(gateway.session_factory) as session:
        user = await get_active_admin_by_token(
            session,
            presented,
            gateway.settings.key_pepper,
        )
    if user is None:
        raise AuthenticationError("Invalid admin credentials", code="invalid_admin")
    return AdminContext(actor=str(user.id), role=user.role, is_master=False)


async def require_admin_write(admin: AdminContext = Depends(require_admin)) -> AdminContext:
    if admin.role != "admin":
        raise InvalidRequestError("Viewer role cannot mutate resources", code="forbidden")
    return admin
