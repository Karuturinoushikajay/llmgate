"""Gateway API keys.

The raw key is shown once. We store an HMAC-SHA256 of the key using a server
pepper, plus a short prefix so operators can tell keys apart without the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmgate.storage.models import ApiKey

_KEY_PREFIX = "sk-lg-"
_PREFIX_LEN = 12


def generate_api_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(raw_key: str, pepper: str) -> str:
    return hmac.new(pepper.encode("utf-8"), raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


def key_prefix(raw_key: str) -> str:
    return raw_key[:_PREFIX_LEN]


async def create_api_key(session: AsyncSession, *, name: str, pepper: str) -> tuple[ApiKey, str]:
    raw_key = generate_api_key()
    row = ApiKey(
        name=name,
        key_prefix=key_prefix(raw_key),
        key_hash=hash_api_key(raw_key, pepper),
    )
    session.add(row)
    await session.flush()
    return row, raw_key


async def list_api_keys(session: AsyncSession) -> list[ApiKey]:
    result = await session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))
    return list(result.all())


async def revoke_api_key(session: AsyncSession, key_id: uuid.UUID) -> ApiKey | None:
    row = await session.get(ApiKey, key_id)
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
    return row


async def get_active_key_by_token(
    session: AsyncSession,
    token: str,
    pepper: str,
) -> ApiKey | None:
    digest = hash_api_key(token, pepper)
    result = await session.scalars(select(ApiKey).where(ApiKey.key_hash == digest))
    row = result.one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    row.last_used_at = datetime.now(UTC)
    return row
