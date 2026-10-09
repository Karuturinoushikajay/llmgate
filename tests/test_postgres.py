"""Proves the Alembic revision applies on Postgres, not only SQLite."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from llmgate.storage.db import create_engine
from llmgate.storage.keys import create_api_key, get_active_key_by_token, revoke_api_key

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.postgres
async def test_alembic_upgrade_and_key_roundtrip() -> None:
    url = os.environ.get("LLMGATE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("LLMGATE_TEST_DATABASE_URL is not set")
    env = os.environ.copy()
    env["LLMGATE_DATABASE_URL"] = url
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
    )

    engine = create_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    raw = ""
    key_id = None
    async with factory() as session:
        row, raw = await create_api_key(session, name="ci", pepper="pepper")
        key_id = row.id
        await session.commit()
    async with factory() as session:
        found = await get_active_key_by_token(session, raw, "pepper")
        assert found is not None
        assert found.id == key_id
        await session.commit()
    async with factory() as session:
        revoked = await revoke_api_key(session, key_id)
        assert revoked is not None
        await session.commit()
    async with factory() as session:
        assert await get_active_key_by_token(session, raw, "pepper") is None
    await engine.dispose()
