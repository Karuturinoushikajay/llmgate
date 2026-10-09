"""Small assertions shared by HTTP tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MASTER_KEY = "test-master-key"


def auth_header(api_key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {api_key}"}


def parse_sse(body: str) -> list[dict[str, Any] | str]:
    events: list[dict[str, Any] | str] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[len("data: ") :]
        if data == "[DONE]":
            events.append("[DONE]")
        else:
            payload = json.loads(data)
            assert isinstance(payload, dict)
            events.append(payload)
    return events


def chat_body(model: str, *, stream: bool = False, **extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": stream,
    }
    body.update(extra)
    return body
