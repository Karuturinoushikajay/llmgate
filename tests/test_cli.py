from __future__ import annotations

import json
from pathlib import Path

from tests.helpers import ROOT

from llmgate.cli import run
from llmgate.config import get_settings


def test_cli_create_list_revoke(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "cli.db"
    monkeypatch.setenv("LLMGATE_DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    monkeypatch.setenv("LLMGATE_KEY_PEPPER", "cli-pepper")
    monkeypatch.setenv("LLMGATE_MASTER_KEY", "cli-master")
    monkeypatch.setenv("LLMGATE_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("LLMGATE_ALEMBIC_INI", str(ROOT / "alembic.ini"))
    monkeypatch.setenv("LLMGATE_MODELS_PATH", str(ROOT / "config" / "models.yaml"))
    get_settings.cache_clear()
    try:
        assert run(["keys", "create", "--name", "demo"]) == 0
        created = _last_json(capsys.readouterr().out)
        assert created["key"].startswith("sk-lg-")
        assert created["name"] == "demo"

        assert run(["keys", "list"]) == 0
        listed = _last_json(capsys.readouterr().out)
        assert listed["data"][0]["id"] == created["id"]
        assert "key" not in listed["data"][0]

        assert run(["keys", "revoke", created["id"]]) == 0
        revoked = _last_json(capsys.readouterr().out)
        assert revoked["revoked_at"] is not None

        assert run(["keys", "revoke", "not-a-uuid"]) == 1
    finally:
        get_settings.cache_clear()


def _last_json(output: str) -> dict[str, object]:
    lines = [line for line in output.splitlines() if line.startswith("{")]
    assert lines, output
    payload = json.loads(lines[-1])
    assert isinstance(payload, dict)
    return payload
