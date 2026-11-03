"""Dashboard operators with admin or viewer roles."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmgate.storage.models import AdminUser

_ROLES = frozenset({"admin", "viewer"})


def generate_admin_token() -> str:
    return "lg-adm-" + secrets.token_urlsafe(32)


def hash_admin_token(raw: str, pepper: str) -> str:
    return hmac.new(pepper.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()


async def create_admin_user(
    session: AsyncSession,
    *,
    email: str,
    role: str,
    pepper: str,
) -> tuple[AdminUser, str]:
    if role not in _ROLES:
        raise ValueError(f"role must be one of {sorted(_ROLES)}")
    raw = generate_admin_token()
    row = AdminUser(email=email, token_hash=hash_admin_token(raw, pepper), role=role)
    session.add(row)
    await session.flush()
    return row, raw


async def list_admin_users(session: AsyncSession) -> list[AdminUser]:
    result = await session.scalars(select(AdminUser).order_by(AdminUser.created_at.desc()))
    return list(result.all())


async def get_active_admin_by_token(
    session: AsyncSession,
    token: str,
    pepper: str,
) -> AdminUser | None:
    digest = hash_admin_token(token, pepper)
    result = await session.scalars(select(AdminUser).where(AdminUser.token_hash == digest))
    row = result.one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    return row


async def revoke_admin_user(session: AsyncSession, user_id: uuid.UUID) -> AdminUser | None:
    row = await session.get(AdminUser, user_id)
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
    return row
