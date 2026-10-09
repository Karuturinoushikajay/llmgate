"""OpenAI chat-completions request and response shapes."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @model_validator(mode="after")
    def fill_total(self) -> Usage:
        if self.total_tokens == 0 and (self.prompt_tokens or self.completion_tokens):
            self.total_tokens = self.prompt_tokens + self.completion_tokens
        return self


class ImageURL(BaseModel):
    model_config = ConfigDict(extra="allow")

    url: str
    detail: str | None = None


class ContentPart(BaseModel):
    """A content part. Unknown part types are preserved for OpenAI passthrough."""

    model_config = ConfigDict(extra="allow")

    type: str
    text: str | None = None
    image_url: ImageURL | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool", "developer"]
    content: str | list[ContentPart] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="allow")

    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    """Subset of the OpenAI chat completions request we route.

    Unknown fields are ignored so newer SDK releases do not 422 the gateway.
    Provider-specific fields are forwarded only by the OpenAI adapter.
    """

    model_config = ConfigDict(extra="ignore")

    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    n: int | None = Field(default=None, ge=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    stop: str | list[str] | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    user: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None
    seed: int | None = None

    def max_output_tokens(self) -> int | None:
        if self.max_completion_tokens is not None:
            return self.max_completion_tokens
        return self.max_tokens

    def stop_sequences(self) -> list[str] | None:
        if self.stop is None:
            return None
        if isinstance(self.stop, str):
            return [self.stop]
        return list(self.stop)

    @property
    def include_usage(self) -> bool:
        return self.stream_options is not None and self.stream_options.include_usage


class ChatCompletionMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class Choice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = 0
    message: ChatCompletionMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage | None = None


class ChoiceDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str | None = None
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChunkChoice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = 0
    delta: ChoiceDelta = Field(default_factory=ChoiceDelta)
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice] = Field(default_factory=list)
    usage: Usage | None = None


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]


def new_completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def unix_timestamp() -> int:
    return int(time.time())


def completion_response(
    *,
    completion_id: str,
    created: int,
    model: str,
    content: str | None,
    finish_reason: str | None,
    usage: Usage,
    tool_calls: list[dict[str, Any]] | None = None,
) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=model,
        choices=[
            Choice(
                index=0,
                message=ChatCompletionMessage(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        usage=usage,
    )


def completion_chunk(
    *,
    completion_id: str,
    created: int,
    model: str,
    content: str | None = None,
    role: str | None = None,
    finish_reason: str | None = None,
    usage: Usage | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=completion_id,
        created=created,
        model=model,
        choices=[
            ChunkChoice(
                index=0,
                delta=ChoiceDelta(role=role, content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        usage=usage,
    )
