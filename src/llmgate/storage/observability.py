"""Append-only request logs and audit events."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llmgate.storage.models import AuditEvent, RequestLog


async def append_request_log(session: AsyncSession, **fields: Any) -> RequestLog:
    row = RequestLog(**fields)
    session.add(row)
    await session.flush()
    return row


async def append_audit_event(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditEvent:
    row = AuditEvent(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=None if detail is None else json.dumps(detail, default=str),
    )
    session.add(row)
    await session.flush()
    return row


async def list_request_logs(
    session: AsyncSession,
    *,
    limit: int = 100,
    team_id: uuid.UUID | None = None,
) -> list[RequestLog]:
    stmt = select(RequestLog).order_by(RequestLog.created_at.desc())
    if team_id is not None:
        stmt = stmt.where(RequestLog.team_id == team_id)
    result = await session.scalars(stmt.limit(limit))
    return list(result.all())


async def list_audit_events(session: AsyncSession, *, limit: int = 100) -> list[AuditEvent]:
    result = await session.scalars(
        select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
    )
    return list(result.all())


async def usage_summary(
    session: AsyncSession,
    *,
    since: datetime | None = None,
) -> dict[str, Any]:
    stmt = select(
        func.count(RequestLog.id),
        func.coalesce(func.sum(RequestLog.total_tokens), 0),
        func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
    )
    if since is not None:
        stmt = stmt.where(RequestLog.created_at >= since)
    count, tokens, cost = (await session.execute(stmt)).one()
    by_model = await session.execute(
        select(
            RequestLog.model,
            func.count(RequestLog.id),
            func.coalesce(func.sum(RequestLog.total_tokens), 0),
            func.coalesce(func.sum(RequestLog.estimated_cost_usd), 0.0),
        )
        .where(RequestLog.model.is_not(None))
        .group_by(RequestLog.model)
        .order_by(func.count(RequestLog.id).desc())
    )
    return {
        "requests": int(count or 0),
        "total_tokens": int(tokens or 0),
        "estimated_cost_usd": float(cost or 0),
        "by_model": [
            {
                "model": model,
                "requests": int(reqs or 0),
                "total_tokens": int(toks or 0),
                "estimated_cost_usd": float(dollars or 0),
            }
            for model, reqs, toks, dollars in by_model.all()
        ],
    }
