"""Admin CLI for gateway API keys.

Examples::

    llmgate keys create --name demo
    llmgate keys list
    llmgate keys revoke <id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

from llmgate.config import get_settings
from llmgate.core.errors import NotFoundError
from llmgate.logging import configure_logging
from llmgate.storage.db import (
    create_engine,
    create_session_factory,
    session_scope,
    upgrade_database,
)
from llmgate.storage.keys import create_api_key, list_api_keys, revoke_api_key
from llmgate.storage.models import ApiKey


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run(argv))


def run(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(_dispatch(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llmgate", description="LLMGate admin commands")
    sub = parser.add_subparsers(dest="command", required=True)
    keys = sub.add_parser("keys", help="Manage gateway API keys")
    actions = keys.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="Create a key and print it once")
    create.add_argument("--name", required=True)
    create.add_argument(
        "--requests-per-minute",
        type=_non_negative_int,
        default=None,
        help="Override the gateway default. 0 disables the request limit.",
    )
    create.add_argument(
        "--tokens-per-minute",
        type=_non_negative_int,
        default=None,
        help="Override the gateway default. 0 disables the token limit.",
    )
    actions.add_parser("list", help="List keys (secrets are not shown)")
    revoke = actions.add_parser("revoke", help="Revoke a key by id")
    revoke.add_argument("key_id")
    return parser


async def _dispatch(args: argparse.Namespace) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        await upgrade_database(engine, settings.alembic_ini)
        async with session_scope(factory) as session:
            if args.action == "create":
                row, raw_key = await create_api_key(
                    session,
                    name=args.name,
                    pepper=settings.key_pepper,
                    requests_per_minute=args.requests_per_minute,
                    tokens_per_minute=args.tokens_per_minute,
                )
                _print(
                    {
                        "id": str(row.id),
                        "name": row.name,
                        "key": raw_key,
                        "key_prefix": row.key_prefix,
                        "created_at": _iso(row.created_at),
                        "requests_per_minute": row.requests_per_minute,
                        "tokens_per_minute": row.tokens_per_minute,
                    }
                )
            elif args.action == "list":
                rows = await list_api_keys(session)
                _print({"object": "list", "data": [_public_view(row) for row in rows]})
            elif args.action == "revoke":
                try:
                    key_id = uuid.UUID(args.key_id)
                except ValueError as exc:
                    raise NotFoundError("API key not found") from exc
                revoked = await revoke_api_key(session, key_id)
                if revoked is None:
                    raise NotFoundError("API key not found")
                _print(_public_view(revoked))
            else:
                raise RuntimeError(f"Unknown action {args.action}")
    finally:
        await engine.dispose()


def _public_view(row: ApiKey) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "key_prefix": row.key_prefix,
        "created_at": _iso(row.created_at),
        "revoked_at": _iso(row.revoked_at),
        "last_used_at": _iso(row.last_used_at),
        "requests_per_minute": row.requests_per_minute,
        "tokens_per_minute": row.tokens_per_minute,
    }


def _iso(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        result = isoformat()
        if isinstance(result, str):
            return result
    return str(value)


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))
