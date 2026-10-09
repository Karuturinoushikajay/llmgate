"""Translate OpenAI message content into provider-specific payloads."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from llmgate.core.errors import InvalidRequestError
from llmgate.core.schemas import ChatMessage, ContentPart


def message_text(message: ChatMessage) -> str:
    if message.content is None:
        return ""
    if isinstance(message.content, str):
        return message.content
    parts = [part.text for part in message.content if part.type == "text" and part.text]
    return "\n".join(parts)


def iter_parts(message: ChatMessage) -> list[ContentPart] | None:
    if message.content is None or isinstance(message.content, str):
        return None
    return list(message.content)


def parse_data_url(url: str) -> tuple[str, str] | None:
    """Return ``(media_type, base64)`` for a base64 data URL, else None."""
    if not url.startswith("data:"):
        return None
    header, separator, data = url.partition(",")
    if not separator or ";base64" not in header:
        raise InvalidRequestError(
            "Only base64 data URLs are supported for images",
            param="messages",
        )
    media_type = header[len("data:") :].split(";", 1)[0] or "application/octet-stream"
    if not data:
        raise InvalidRequestError("Image data URL is empty", param="messages")
    return media_type, data


def is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def anthropic_blocks(message: ChatMessage) -> str | list[dict[str, Any]]:
    if message.content is None or isinstance(message.content, str):
        return message.content or ""
    parts = message.content
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if part.type == "text":
            blocks.append({"type": "text", "text": part.text or ""})
            continue
        if part.type == "image_url" and part.image_url is not None:
            blocks.append(_anthropic_image(part.image_url.url))
            continue
        raise InvalidRequestError(
            f"Unsupported content part '{part.type}' for anthropic",
            param="messages",
        )
    return blocks


def _anthropic_image(url: str) -> dict[str, Any]:
    parsed = parse_data_url(url)
    if parsed is not None:
        media_type, data = parsed
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    if is_http_url(url):
        return {"type": "image", "source": {"type": "url", "url": url}}
    raise InvalidRequestError(
        "Anthropic image parts need a data URL or http(s) URL",
        param="messages",
    )


def gemini_parts(message: ChatMessage) -> list[dict[str, Any]]:
    parts = iter_parts(message)
    if parts is None:
        return [{"text": message.content or ""}]
    translated: list[dict[str, Any]] = []
    for part in parts:
        if part.type == "text":
            translated.append({"text": part.text or ""})
            continue
        if part.type == "image_url" and part.image_url is not None:
            translated.append(_gemini_image(part.image_url.url))
            continue
        raise InvalidRequestError(
            f"Unsupported content part '{part.type}' for gemini",
            param="messages",
        )
    return translated or [{"text": ""}]


def _gemini_image(url: str) -> dict[str, Any]:
    parsed = parse_data_url(url)
    if parsed is not None:
        media_type, data = parsed
        return {"inline_data": {"mime_type": media_type, "data": data}}
    if is_http_url(url):
        return {"file_data": {"file_uri": url}}
    raise InvalidRequestError("Gemini image parts need a data URL or http(s) URL", param="messages")


def ollama_message(message: ChatMessage, *, role: str) -> dict[str, Any]:
    parts = iter_parts(message)
    if parts is None:
        return {"role": role, "content": message.content or ""}
    texts: list[str] = []
    images: list[str] = []
    for part in parts:
        if part.type == "text":
            if part.text:
                texts.append(part.text)
            continue
        if part.type == "image_url" and part.image_url is not None:
            parsed = parse_data_url(part.image_url.url)
            if parsed is None:
                raise InvalidRequestError(
                    "Ollama image parts need a base64 data URL",
                    param="messages",
                )
            images.append(parsed[1])
            continue
        raise InvalidRequestError(
            f"Unsupported content part '{part.type}' for ollama",
            param="messages",
        )
    payload: dict[str, Any] = {"role": role, "content": "\n".join(texts)}
    if images:
        payload["images"] = images
    return payload
