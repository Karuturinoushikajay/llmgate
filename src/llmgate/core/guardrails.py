"""A small denylist. It is a filter, not a solution to prompt injection."""

from __future__ import annotations

import re

from llmgate.core.schemas import ChatCompletionRequest, ContentPart

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_previous",
        re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions", re.IGNORECASE),
    ),
    (
        "disregard_system",
        re.compile(r"disregard\s+(?:your|the|all)\s+(?:system|previous|prior)", re.IGNORECASE),
    ),
    (
        "reveal_system_prompt",
        re.compile(r"reveal\s+(?:your|the)\s+system\s+prompt", re.IGNORECASE),
    ),
    (
        "jailbreak",
        re.compile(
            r"\bjailbreak\b|you\s+are\s+now\s+(?:a\s+|an\s+)?(?:unfiltered|jailbroken|dan)\b",
            re.IGNORECASE,
        ),
    ),
)


def scan(request: ChatCompletionRequest) -> str | None:
    """Return a rule id when user or tool text matches. System text is skipped."""
    for message in request.messages:
        if message.role not in ("user", "tool"):
            continue
        text = _text(message.content)
        for rule_id, pattern in _RULES:
            if pattern.search(text):
                return rule_id
    return None


def _text(content: str | list[ContentPart] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return " ".join(part.text for part in content if part.text)
