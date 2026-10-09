from __future__ import annotations

from pathlib import Path

import pytest

from llmgate.core.errors import ModelNotFoundError
from llmgate.core.registry import ModelRegistry

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_registry_loads() -> None:
    registry = ModelRegistry.load(ROOT / "config" / "models.yaml", loaded_at=1)
    resolved = registry.resolve("claude-3-5-sonnet")
    assert resolved.provider == "anthropic"
    assert resolved.upstream_model == "claude-3-5-sonnet-20241022"
    assert {entry.id for entry in registry.list_models()} >= {
        "gpt-4o-mini",
        "claude-3-5-sonnet",
        "gemini-2.0-flash",
        "llama3.2",
    }


def test_fallback_chain_is_primary_then_configured_order() -> None:
    registry = ModelRegistry.load(ROOT / "config" / "models.yaml", loaded_at=1)
    resolved = registry.resolve("gpt-4o")
    assert [(target.provider, target.upstream_model) for target in resolved.targets] == [
        ("openai", "gpt-4o"),
        ("anthropic", "claude-3-5-haiku-20241022"),
        ("gemini", "gemini-2.0-flash"),
    ]


def test_provider_prefix_bypasses_registry() -> None:
    registry = ModelRegistry.load(ROOT / "config" / "models.yaml", loaded_at=1)
    resolved = registry.resolve("ollama/llama3.2:latest")
    assert resolved.provider == "ollama"
    assert resolved.upstream_model == "llama3.2:latest"
    assert resolved.public_name == "ollama/llama3.2:latest"
    assert len(resolved.targets) == 1


def test_unknown_model() -> None:
    registry = ModelRegistry.load(ROOT / "config" / "models.yaml", loaded_at=1)
    with pytest.raises(ModelNotFoundError) as exc:
        registry.resolve("does-not-exist")
    assert exc.value.status_code == 404
    assert exc.value.code == "model_not_found"


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        "models:\n"
        "  - {id: a, provider: openai, upstream_model: a}\n"
        "  - {id: a, provider: openai, upstream_model: b}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate"):
        ModelRegistry.load(path, loaded_at=1)
