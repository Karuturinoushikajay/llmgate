"""Estimate USD cost from the pricing YAML. Missing models stay null."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class PricingTable:
    def __init__(self, rates: dict[str, tuple[float, float]]) -> None:
        self._rates = rates

    @classmethod
    def load(cls, path: Path) -> PricingTable:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        models = raw.get("models", {}) if isinstance(raw, dict) else {}
        rates: dict[str, tuple[float, float]] = {}
        if isinstance(models, dict):
            for name, entry in models.items():
                if not isinstance(entry, dict):
                    continue
                prompt = float(entry.get("prompt", 0))
                completion = float(entry.get("completion", 0))
                rates[str(name)] = (prompt, completion)
        return cls(rates)

    def estimate(
        self,
        model: str | None,
        *,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cache: str,
    ) -> float | None:
        if cache == "hit":
            return 0.0
        if model is None or model not in self._rates:
            return None
        prompt_rate, completion_rate = self._rates[model]
        prompt = prompt_tokens or 0
        completion = completion_tokens or 0
        return (prompt * prompt_rate + completion * completion_rate) / 1_000_000


def resolve_pricing_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"Pricing file not found at {candidate}")


def pricing_snapshot(table: PricingTable) -> dict[str, Any]:
    return {
        name: {"prompt": prompt, "completion": completion}
        for name, (prompt, completion) in sorted(table._rates.items())
    }
