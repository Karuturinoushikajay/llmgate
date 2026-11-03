"""Map a public model name to a provider and an upstream model id."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from llmgate.core.errors import ModelNotFoundError

ProviderName = Literal["openai", "anthropic", "gemini", "ollama"]
KNOWN_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic", "gemini", "ollama"})


class ModelRoutes(BaseModel):
    simple: str = Field(min_length=1)
    complex: str = Field(min_length=1)


class ModelEntry(BaseModel):
    id: str = Field(min_length=1)
    provider: ProviderName
    upstream_model: str = Field(min_length=1)
    fallbacks: list[str] = Field(default_factory=list)
    routes: ModelRoutes | None = None


class ResolvedModel(BaseModel):
    public_name: str
    provider: ProviderName
    upstream_model: str


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
        _validate_fallbacks(path, entries)
        _validate_routes(path, entries)
        return cls(entries, loaded_at=loaded_at)

    def get(self, model_id: str) -> ModelEntry | None:
        return self._models.get(model_id)

    def resolve(self, name: str) -> ResolvedModel:
        """Resolve ``name``.

        A slash selects the provider explicitly (``anthropic/claude-...``),
        including models that are not listed in the registry. Otherwise the
        name must match a registry id.
        """
        if "/" in name:
            provider, _, upstream = name.partition("/")
            if provider in KNOWN_PROVIDERS and upstream:
                return ResolvedModel(
                    public_name=name,
                    provider=provider,  # type: ignore[arg-type]
                    upstream_model=upstream,
                )
        entry = self._models.get(name)
        if entry is None:
            raise ModelNotFoundError(name)
        return ResolvedModel(
            public_name=entry.id,
            provider=entry.provider,
            upstream_model=entry.upstream_model,
        )

    def list_models(self) -> list[ModelEntry]:
        return list(self._models.values())


def _validate_fallbacks(path: Path, entries: list[ModelEntry]) -> None:
    ids = {entry.id for entry in entries}
    for entry in entries:
        for name in entry.fallbacks:
            if name == entry.id:
                raise ValueError(f"{entry.id} in {path} cannot fall back to itself")
            if not name.strip():
                raise ValueError(f"Empty fallback for {entry.id} in {path}")
            if "/" in name:
                provider, _, upstream = name.partition("/")
                if provider not in KNOWN_PROVIDERS or not upstream:
                    raise ValueError(f"Invalid fallback '{name}' for {entry.id} in {path}")
            elif name not in ids:
                raise ValueError(f"Unknown fallback '{name}' for {entry.id} in {path}")


def _validate_routes(path: Path, entries: list[ModelEntry]) -> None:
    ids = {entry.id for entry in entries}
    for entry in entries:
        if entry.routes is None:
            continue
        for name in (entry.routes.simple, entry.routes.complex):
            if name == entry.id:
                raise ValueError(f"{entry.id} in {path} cannot route to itself")
            if "/" in name or name not in ids:
                raise ValueError(f"Unknown route '{name}' for {entry.id} in {path}")
