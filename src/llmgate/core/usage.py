"""Normalize provider usage payloads into OpenAI token counts."""

from __future__ import annotations

from typing import Any

from llmgate.core.schemas import Usage


def usage_from_openai(payload: dict[str, Any] | None) -> Usage:
    if not payload:
        return Usage()
    return Usage(
        prompt_tokens=int(payload.get("prompt_tokens") or 0),
        completion_tokens=int(payload.get("completion_tokens") or 0),
        total_tokens=int(payload.get("total_tokens") or 0),
    )


def usage_from_anthropic(
    payload: dict[str, Any] | None,
    *,
    output_tokens: int | None = None,
) -> Usage:
    data = payload or {}
    prompt = int(data.get("input_tokens") or 0)
    completion = output_tokens if output_tokens is not None else int(data.get("output_tokens") or 0)
    return Usage(prompt_tokens=prompt, completion_tokens=completion)


def usage_from_gemini(payload: dict[str, Any] | None) -> Usage:
    if not payload:
        return Usage()
    return Usage(
        prompt_tokens=int(payload.get("promptTokenCount") or 0),
        completion_tokens=int(payload.get("candidatesTokenCount") or 0),
        total_tokens=int(payload.get("totalTokenCount") or 0),
    )


def usage_from_ollama(payload: dict[str, Any]) -> Usage:
    return Usage(
        prompt_tokens=int(payload.get("prompt_eval_count") or 0),
        completion_tokens=int(payload.get("eval_count") or 0),
    )
