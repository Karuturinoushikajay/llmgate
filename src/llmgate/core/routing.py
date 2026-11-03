"""Complexity heuristic for the public ``auto`` model."""

from __future__ import annotations

from typing import Literal

RouteTier = Literal["simple", "complex"]


def complexity(text: str, message_count: int, *, limit: int) -> RouteTier:
    """Simple prompts are short, few, and not code.

    A code fence or a long transcript is complex even when the character
    count would otherwise pass.
    """
    if message_count > 2 or len(text) > limit or "```" in text:
        return "complex"
    return "simple"
