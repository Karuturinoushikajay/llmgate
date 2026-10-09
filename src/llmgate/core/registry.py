"""Map a public model name to a provider and an upstream model id."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from llmgate.core.errors import ModelNotFoundError

ProviderName = Literal["openai", "anthropic", "gemini", "ollama"]
KNOWN_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic", "gemini", "ollama"})


class ModelTarget(BaseModel):
    provider: ProviderName
    upstream_model: str = Field(min_length=1)


class ModelEntry(BaseModel):
    id: str = Field(min_length=1)
    provider: ProviderName
    upstream_model: str = Field(min_length=1)
    fallbacks: list[ModelTarget] = Field(default_factory=list)


class ResolvedModel(BaseModel):
    public_name: str
    provider: ProviderName
    upstream_model: str
    targets: list[ModelTarget]


class ModelRegistry:
    def __init__(self, models: list[ModelEntry], *, loaded_at: int) -> None:
        self._models = {model.id: model for model in models}
        self.loaded_at = loaded_at

    @classmethod
    def load(cls, path: Path, *, loaded_at: int) -> ModelRegistry:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict) or not isinstance(raw.get("models"), list):
            raise ValueError(f"{path} must contain a 'models' list")
        entries = [ModelEntry.model_validate(item) for item in raw["models"]]
        ids = [entry.id for entry in entries]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate model ids in {path}")
        return cls(entries, loaded_at=loaded_at)

    def resolve(self, name: str) -> ResolvedModel:
        """Resolve ``name``.

        A slash selects the provider explicitly (``anthropic/claude-...``),
        including models that are not listed in the registry. Otherwise the
        name must match a registry id.
        """
        if "/" in name:
            provider, _, upstream = name.partition("/")
            if provider in KNOWN_PROVIDERS and upstream:
                target = ModelTarget(
                    provider=provider,  # type: ignore[arg-type]
                    upstream_model=upstream,
                )
                return ResolvedModel(
                    public_name=name,
                    provider=target.provider,
                    upstream_model=target.upstream_model,
                    targets=[target],
                )
        entry = self._models.get(name)
        if entry is None:
            raise ModelNotFoundError(name)
        primary = ModelTarget(provider=entry.provider, upstream_model=entry.upstream_model)
        return ResolvedModel(
            public_name=entry.id,
            provider=entry.provider,
            upstream_model=entry.upstream_model,
            targets=[primary, *entry.fallbacks],
        )

    def list_models(self) -> list[ModelEntry]:
        return list(self._models.values())
