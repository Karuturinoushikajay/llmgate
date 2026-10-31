"""Semantic cache: cosine similarity over stored chat completions."""

from __future__ import annotations

import json
import math
import struct
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from llmgate.core.schemas import ChatCompletionResponse

_TAG_ESCAPES = set(r",.<>{}[]\"':;!@#$%^&*()-+=~|/\\ ")


class VectorRedis(Protocol):
    def execute_command(self, *args: Any, **kwargs: Any) -> Awaitable[Any]: ...


@dataclass(frozen=True)
class CacheHit:
    response: ChatCompletionResponse
    score: float


class SemanticCache(Protocol):
    def lookup(self, *, model: str, vector: list[float]) -> Awaitable[CacheHit | None]: ...

    def store(
        self,
        *,
        model: str,
        vector: list[float],
        response: ChatCompletionResponse,
    ) -> Awaitable[None]: ...


@dataclass
class _MemoryEntry:
    model: str
    vector: list[float]
    response: ChatCompletionResponse
    expires_at: float


class MemoryCache:
    """Cosine index used by tests. FakeAsyncRedis cannot run RediSearch."""

    def __init__(
        self,
        *,
        similarity: float,
        ttl_seconds: float,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.similarity = similarity
        self.ttl_seconds = ttl_seconds
        self.now: Callable[[], float] = time.monotonic if now is None else now
        self._entries: list[_MemoryEntry] = []

    async def lookup(self, *, model: str, vector: list[float]) -> CacheHit | None:
        now = float(self.now())
        best: CacheHit | None = None
        for entry in self._entries:
            if entry.model != model or entry.expires_at <= now:
                continue
            score = cosine_similarity(vector, entry.vector)
            if score < self.similarity:
                continue
            if best is None or score > best.score:
                best = CacheHit(response=entry.response.model_copy(deep=True), score=score)
        return best

    async def store(
        self,
        *,
        model: str,
        vector: list[float],
        response: ChatCompletionResponse,
    ) -> None:
        self._entries.append(
            _MemoryEntry(
                model=model,
                vector=list(vector),
                response=response.model_copy(deep=True),
                expires_at=float(self.now()) + self.ttl_seconds,
            )
        )


class RedisSemanticCache:
    """KNN search on a Redis Stack vector index."""

    def __init__(self, redis: VectorRedis, *, similarity: float, ttl_seconds: int) -> None:
        self.redis = redis
        self.similarity = similarity
        self.ttl_seconds = ttl_seconds
        self._ready: set[str] = set()

    async def lookup(self, *, model: str, vector: list[float]) -> CacheHit | None:
        index = _index_name(len(vector))
        try:
            raw = await self.redis.execute_command(
                "FT.SEARCH",
                index,
                f"(@chat_model:{{{_escape_tag(model)}}})=>[KNN 1 @embedding $vec AS dist]",
                "PARAMS",
                2,
                "vec",
                _blob(vector),
                "SORTBY",
                "dist",
                "RETURN",
                2,
                "completion",
                "dist",
                "DIALECT",
                2,
                "LIMIT",
                0,
                1,
            )
        except Exception as exc:
            if _missing_index(exc):
                self._ready.discard(index)
                return None
            raise
        found = _parse_hit(raw, self.similarity)
        return found

    async def store(
        self,
        *,
        model: str,
        vector: list[float],
        response: ChatCompletionResponse,
    ) -> None:
        await self._ensure_index(len(vector))
        key = f"llmgate:cache:doc:{uuid.uuid4().hex}"
        await self.redis.execute_command(
            "HSET",
            key,
            "chat_model",
            model,
            "completion",
            response.model_dump_json(),
            "embedding",
            _blob(vector),
        )
        await self.redis.execute_command("EXPIRE", key, self.ttl_seconds)

    async def _ensure_index(self, dimensions: int) -> None:
        index = _index_name(dimensions)
        if index in self._ready:
            return
        try:
            await self.redis.execute_command(
                "FT.CREATE",
                index,
                "ON",
                "HASH",
                "PREFIX",
                1,
                "llmgate:cache:doc:",
                "SCHEMA",
                "chat_model",
                "TAG",
                "completion",
                "TEXT",
                "embedding",
                "VECTOR",
                "HNSW",
                6,
                "TYPE",
                "FLOAT32",
                "DIM",
                dimensions,
                "DISTANCE_METRIC",
                "COSINE",
            )
        except Exception as exc:
            if not _index_exists(exc):
                raise
        self._ready.add(index)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for first, second in zip(left, right, strict=True):
        dot += first * second
        left_norm += first * first
        right_norm += second * second
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)


def _blob(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _index_name(dimensions: int) -> str:
    return f"llmgate:cache:idx:{dimensions}"


def _escape_tag(value: str) -> str:
    escaped: list[str] = []
    for char in value:
        if char in _TAG_ESCAPES:
            escaped.append("\\")
        escaped.append(char)
    return "".join(escaped)


def _missing_index(exc: Exception) -> bool:
    text = str(exc).lower()
    return "unknown index" in text or "no such index" in text


def _index_exists(exc: Exception) -> bool:
    return "index already exists" in str(exc).lower()


def _parse_hit(raw: object, similarity: float) -> CacheHit | None:
    completion, distance = _fields(raw)
    if completion is None or distance is None:
        return None
    score = 1.0 - distance
    if score < similarity:
        return None
    try:
        payload = json.loads(completion)
    except json.JSONDecodeError:
        return None
    return CacheHit(response=ChatCompletionResponse.model_validate(payload), score=score)


def _fields(raw: object) -> tuple[str | None, float | None]:
    if isinstance(raw, dict):
        results = raw.get("results")
        if not isinstance(results, list) or not results:
            return None, None
        first = results[0]
        if not isinstance(first, dict):
            return None, None
        attributes = first.get("extra_attributes")
        if not isinstance(attributes, dict):
            attributes = first
        return _pair(attributes.get("completion"), attributes.get("dist", attributes.get("score")))
    if not isinstance(raw, list) or len(raw) < 3:
        return None, None
    fields = raw[2]
    if not isinstance(fields, list):
        return None, None
    mapped: dict[str, object] = {}
    for index in range(0, len(fields) - 1, 2):
        key = fields[index]
        if isinstance(key, bytes):
            key = key.decode()
        if isinstance(key, str):
            mapped[key] = fields[index + 1]
    return _pair(mapped.get("completion"), mapped.get("dist", mapped.get("score")))


def _pair(completion: object, distance: object) -> tuple[str | None, float | None]:
    if isinstance(completion, bytes):
        completion = completion.decode()
    if isinstance(distance, bytes):
        distance = distance.decode()
    if not isinstance(completion, str):
        return None, None
    try:
        parsed = float(str(distance))
    except (TypeError, ValueError):
        return None, None
    return completion, parsed
