"""Rough token estimate used to reserve TPM before the provider responds.

The real count replaces this reservation after the call. Four characters per
token is the usual English approximation; it only has to be in the right
order of magnitude so a burst cannot all pass the limiter before usage returns.
"""

from __future__ import annotations

from llmgate.core.schemas import ChatCompletionRequest


def estimate_tokens(request: ChatCompletionRequest, *, output_reservation: int) -> int:
    chars = 0
    for message in request.messages:
        content = message.content
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if part.text:
                    chars += len(part.text)
    prompt = max(1, chars // 4)
    output = request.max_output_tokens() or output_reservation
    return prompt + max(0, output)
