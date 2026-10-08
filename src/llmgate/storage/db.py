"""Async engine, sessions, and Alembic startup migrations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


def create_engine(database_url: str) -> AsyncEngine:
    kwargs: dict[str, object] = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in database_url or database_url.rstrip("/") in {
            "sqlite+aiosqlite:",
            "sqlite+aiosqlite://",
        }:
            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_pre_ping"] = True
    return create_async_engine(database_url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def upgrade_database(engine: AsyncEngine, alembic_ini: str) -> None:
    """Apply Alembic migrations on the application engine."""

    def _upgrade(connection: Connection) -> None:
        ini_path = Path(alembic_ini).resolve()
        cfg = Config(str(ini_path))
        # Resolve relative to the ini file so the CLI works from any cwd.
        cfg.set_main_option("script_location", str(ini_path.parent / "alembic"))
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")

    async with engine.connect() as connection:
        await connection.run_sync(_upgrade)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
