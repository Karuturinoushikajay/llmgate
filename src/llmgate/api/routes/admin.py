"""Admin APIs for keys, teams, usage, audit, routing, and dashboard users."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from llmgate.api.deps import AdminContext, get_gateway, require_admin, require_admin_write
from llmgate.core.errors import InvalidRequestError, NotFoundError
from llmgate.core.pricing import pricing_snapshot
from llmgate.storage.admin_users import create_admin_user, list_admin_users, revoke_admin_user
from llmgate.storage.db import session_scope
from llmgate.storage.keys import (
    create_api_key,
    create_team,
    list_api_keys,
    list_teams,
    revoke_api_key,
)
from llmgate.storage.models import AdminUser, ApiKey, AuditEvent, RequestLog, Team
from llmgate.storage.observability import (
    append_audit_event,
    list_audit_events,
    list_request_logs,
    usage_summary,
)

router = APIRouter(prefix="/admin", tags=["admin"])


class CreateTeamRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    monthly_token_budget: int | None = Field(default=None, ge=0)


class TeamView(BaseModel):
    id: uuid.UUID
    name: str
    monthly_token_budget: int | None
    created_at: datetime


class TeamList(BaseModel):
    object: str = "list"
    data: list[TeamView]


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    team_id: uuid.UUID | None = None


class KeyView(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    team_id: uuid.UUID | None
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None


class KeyCreated(KeyView):
    key: str
    message: str = "Store this key now. It will not be shown again."


class KeyList(BaseModel):
    object: str = "list"
    data: list[KeyView]


class CreateAdminUserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    role: Literal["admin", "viewer"] = "viewer"


class AdminUserView(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    created_at: datetime
    revoked_at: datetime | None


class AdminUserCreated(AdminUserView):
    token: str
    message: str = "Store this token now. It will not be shown again."


class AdminUserList(BaseModel):
    object: str = "list"
    data: list[AdminUserView]


class RequestLogView(BaseModel):
    id: uuid.UUID
    request_id: str
    api_key_id: uuid.UUID | None
    team_id: uuid.UUID | None
    method: str
    path: str
    status_code: int
    latency_ms: float
    model: str | None
    provider: str | None
    route: str | None
    cache: str
    stream: bool
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    estimated_cost_usd: float | None
    error: str | None
    created_at: datetime


class RequestLogList(BaseModel):
    object: str = "list"
    data: list[RequestLogView]


class AuditView(BaseModel):
    id: uuid.UUID
    actor: str
    action: str
    resource_type: str
    resource_id: str | None
    detail: str | None
    created_at: datetime


class AuditList(BaseModel):
    object: str = "list"
    data: list[AuditView]


def _view(row: ApiKey) -> KeyView:
    return KeyView(
        id=row.id,
        name=row.name,
        key_prefix=row.key_prefix,
        team_id=row.team_id,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
    )


def _team(row: Team) -> TeamView:
    return TeamView(
        id=row.id,
        name=row.name,
        monthly_token_budget=row.monthly_token_budget,
        created_at=row.created_at,
    )


def _user_view(row: AdminUser) -> AdminUserView:
    return AdminUserView(
        id=row.id,
        email=row.email,
        role=row.role,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


def _log(row: RequestLog) -> RequestLogView:
    return RequestLogView(
        id=row.id,
        request_id=row.request_id,
        api_key_id=row.api_key_id,
        team_id=row.team_id,
        method=row.method,
        path=row.path,
        status_code=row.status_code,
        latency_ms=row.latency_ms,
        model=row.model,
        provider=row.provider,
        route=row.route,
        cache=row.cache,
        stream=row.stream,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        total_tokens=row.total_tokens,
        estimated_cost_usd=row.estimated_cost_usd,
        error=row.error,
        created_at=row.created_at,
    )


def _audit(row: AuditEvent) -> AuditView:
    return AuditView(
        id=row.id,
        actor=row.actor,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        detail=row.detail,
        created_at=row.created_at,
    )


@router.post("/teams", status_code=201)
async def create_team_route(
    body: CreateTeamRequest,
    request: Request,
    admin: AdminContext = Depends(require_admin_write),
) -> TeamView:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        row = await create_team(
            session,
            name=body.name,
            monthly_token_budget=body.monthly_token_budget,
        )
        await append_audit_event(
            session,
            actor=admin.actor,
            action="create",
            resource_type="team",
            resource_id=str(row.id),
            detail={"name": row.name, "monthly_token_budget": row.monthly_token_budget},
        )
        return _team(row)


@router.get("/teams")
async def list_teams_route(
    request: Request,
    _admin: AdminContext = Depends(require_admin),
) -> TeamList:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        rows = await list_teams(session)
        return TeamList(data=[_team(row) for row in rows])


@router.post("/keys", status_code=201)
async def create_key(
    body: CreateKeyRequest,
    request: Request,
    admin: AdminContext = Depends(require_admin_write),
) -> KeyCreated:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        if body.team_id is not None and await session.get(Team, body.team_id) is None:
            raise NotFoundError("Team not found")
        row, raw_key = await create_api_key(
            session,
            name=body.name,
            pepper=gateway.settings.key_pepper,
            team_id=body.team_id,
        )
        await append_audit_event(
            session,
            actor=admin.actor,
            action="create",
            resource_type="api_key",
            resource_id=str(row.id),
            detail={"name": row.name, "team_id": str(row.team_id) if row.team_id else None},
        )
        return KeyCreated(
            id=row.id,
            name=row.name,
            key_prefix=row.key_prefix,
            team_id=row.team_id,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
            last_used_at=row.last_used_at,
            key=raw_key,
        )


@router.get("/keys")
async def list_keys(
    request: Request,
    _admin: AdminContext = Depends(require_admin),
) -> KeyList:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        rows = await list_api_keys(session)
        return KeyList(data=[_view(row) for row in rows])


@router.delete("/keys/{key_id}")
async def revoke_key(
    key_id: uuid.UUID,
    request: Request,
    admin: AdminContext = Depends(require_admin_write),
) -> KeyView:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        row = await revoke_api_key(session, key_id)
        if row is None:
            raise NotFoundError("API key not found")
        await append_audit_event(
            session,
            actor=admin.actor,
            action="revoke",
            resource_type="api_key",
            resource_id=str(row.id),
            detail={"name": row.name},
        )
        return _view(row)


@router.post("/users", status_code=201)
async def create_user(
    body: CreateAdminUserRequest,
    request: Request,
    admin: AdminContext = Depends(require_admin_write),
) -> AdminUserCreated:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        try:
            row, raw = await create_admin_user(
                session,
                email=body.email,
                role=body.role,
                pepper=gateway.settings.key_pepper,
            )
        except ValueError as exc:
            raise InvalidRequestError(str(exc), param="role") from exc
        await append_audit_event(
            session,
            actor=admin.actor,
            action="create",
            resource_type="admin_user",
            resource_id=str(row.id),
            detail={"email": row.email, "role": row.role},
        )
        return AdminUserCreated(
            id=row.id,
            email=row.email,
            role=row.role,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
            token=raw,
        )


@router.get("/users")
async def list_users(
    request: Request,
    _admin: AdminContext = Depends(require_admin),
) -> AdminUserList:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        rows = await list_admin_users(session)
        return AdminUserList(data=[_user_view(row) for row in rows])


@router.delete("/users/{user_id}")
async def revoke_user(
    user_id: uuid.UUID,
    request: Request,
    admin: AdminContext = Depends(require_admin_write),
) -> AdminUserView:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        row = await revoke_admin_user(session, user_id)
        if row is None:
            raise NotFoundError("Admin user not found")
        await append_audit_event(
            session,
            actor=admin.actor,
            action="revoke",
            resource_type="admin_user",
            resource_id=str(row.id),
            detail={"email": row.email},
        )
        return _user_view(row)


@router.get("/logs")
async def get_logs(
    request: Request,
    _admin: AdminContext = Depends(require_admin),
    limit: int = Query(default=100, ge=1, le=500),
    team_id: uuid.UUID | None = None,
) -> RequestLogList:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        rows = await list_request_logs(session, limit=limit, team_id=team_id)
        return RequestLogList(data=[_log(row) for row in rows])


@router.get("/audit")
async def get_audit(
    request: Request,
    _admin: AdminContext = Depends(require_admin),
    limit: int = Query(default=100, ge=1, le=500),
) -> AuditList:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        rows = await list_audit_events(session, limit=limit)
        return AuditList(data=[_audit(row) for row in rows])


@router.get("/usage")
async def get_usage(
    request: Request,
    _admin: AdminContext = Depends(require_admin),
) -> dict[str, Any]:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        return await usage_summary(session)


@router.get("/routing")
async def get_routing(
    request: Request,
    _admin: AdminContext = Depends(require_admin),
) -> dict[str, Any]:
    gateway = get_gateway(request)
    models = []
    for entry in gateway.registry.list_models():
        models.append(
            {
                "id": entry.id,
                "provider": entry.provider,
                "upstream_model": entry.upstream_model,
                "fallbacks": entry.fallbacks,
                "routes": None
                if entry.routes is None
                else {"simple": entry.routes.simple, "complex": entry.routes.complex},
            }
        )
    return {
        "models": models,
        "pricing": pricing_snapshot(gateway.pricing),
        "route_simple_chars": gateway.settings.route_simple_chars,
    }


@router.get("/me")
async def admin_me(admin: AdminContext = Depends(require_admin)) -> dict[str, Any]:
    return {"actor": admin.actor, "role": admin.role, "is_master": admin.is_master}
