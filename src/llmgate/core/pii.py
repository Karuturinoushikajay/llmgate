"""Replace common PII with placeholders before a prompt leaves the gateway."""

from __future__ import annotations

import re

from llmgate.core.schemas import ChatCompletionRequest, ContentPart

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+1[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")


def mask_request(request: ChatCompletionRequest) -> ChatCompletionRequest:
    messages = [
        message.model_copy(update={"content": _mask_content(message.content)})
        for message in request.messages
    ]
    return request.model_copy(update={"messages": messages})


def mask_text(text: str) -> str:
    text = _EMAIL.sub("[EMAIL]", text)
    text = _SSN.sub("[SSN]", text)
    text = _CARD.sub(_card, text)
    return _PHONE.sub("[PHONE]", text)


def _mask_content(content: str | list[ContentPart] | None) -> str | list[ContentPart] | None:
    if content is None:
        return None
    if isinstance(content, str):
        return mask_text(content)
    return [
        part.model_copy(update={"text": mask_text(part.text) if part.text else part.text})
        for part in content
    ]


def _card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and _luhn(digits):
        return "[CARD]"
    return match.group(0)


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0
