"""Master-key endpoints for creating, listing, and revoking gateway API keys."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from llmgate.api.deps import get_gateway, require_master_key
from llmgate.core.errors import NotFoundError
from llmgate.storage.db import session_scope
from llmgate.storage.keys import create_api_key, list_api_keys, revoke_api_key
from llmgate.storage.models import ApiKey

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_master_key)])


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    requests_per_minute: int | None = Field(default=None, ge=0)
    tokens_per_minute: int | None = Field(default=None, ge=0)


class KeyView(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    requests_per_minute: int | None
    tokens_per_minute: int | None


class KeyCreated(KeyView):
    key: str
    message: str = "Store this key now. It will not be shown again."


class KeyList(BaseModel):
    object: str = "list"
    data: list[KeyView]


def _view(row: ApiKey) -> KeyView:
    return KeyView(
        id=row.id,
        name=row.name,
        key_prefix=row.key_prefix,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
        requests_per_minute=row.requests_per_minute,
        tokens_per_minute=row.tokens_per_minute,
    )


@router.post("/keys", status_code=201)
async def create_key(body: CreateKeyRequest, request: Request) -> KeyCreated:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        row, raw_key = await create_api_key(
            session,
            name=body.name,
            pepper=gateway.settings.key_pepper,
            requests_per_minute=body.requests_per_minute,
            tokens_per_minute=body.tokens_per_minute,
        )
        created = KeyCreated(
            id=row.id,
            name=row.name,
            key_prefix=row.key_prefix,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
            last_used_at=row.last_used_at,
            requests_per_minute=row.requests_per_minute,
            tokens_per_minute=row.tokens_per_minute,
            key=raw_key,
        )
    return created


@router.get("/keys")
async def list_keys(request: Request) -> KeyList:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        rows = await list_api_keys(session)
        return KeyList(data=[_view(row) for row in rows])


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: uuid.UUID, request: Request) -> KeyView:
    gateway = get_gateway(request)
    async with session_scope(gateway.session_factory) as session:
        row = await revoke_api_key(session, key_id)
        if row is None:
            raise NotFoundError("API key not found")
        return _view(row)
