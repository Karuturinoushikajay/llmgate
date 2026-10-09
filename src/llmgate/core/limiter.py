"""Token-bucket limits for requests per minute and tokens per minute.

Both buckets are decided in one Lua script. Redis runs the script atomically,
so two gateway processes cannot both spend the last token. A denied call
writes nothing: the request is not charged when it is rejected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from llmgate.core.errors import ProviderError, RateLimitError
from llmgate.core.redis_client import RedisClient

_WINDOW_MS = 60_000

_ACQUIRE = """
local req_key = KEYS[1]
local tok_key = KEYS[2]
local now = tonumber(ARGV[1])
local req_capacity = tonumber(ARGV[2])
local req_cost = tonumber(ARGV[3])
local tok_capacity = tonumber(ARGV[4])
local tok_cost = tonumber(ARGV[5])
local window = tonumber(ARGV[6])

local function refill(key, capacity)
  if capacity <= 0 then
    return 0, 0
  end
  local raw = redis.call('HGET', key, 'tokens')
  local updated = tonumber(redis.call('HGET', key, 'updated_ms') or '0')
  local tokens
  if not raw then
    tokens = capacity
    updated = now
  else
    tokens = tonumber(raw)
  end
  local elapsed = math.max(0, now - updated)
  local rate = capacity / window
  tokens = math.min(capacity, tokens + elapsed * rate)
  return tokens, rate
end

local function plan(tokens, rate, capacity, cost)
  if capacity <= 0 then
    return 1, -1, 0, 0, tokens
  end
  local allowed = 0
  local retry_ms = 0
  local new_tokens = tokens
  if tokens >= cost then
    allowed = 1
    new_tokens = tokens - cost
  else
    local deficit = cost - tokens
    if rate > 0 then
      retry_ms = math.ceil(deficit / rate)
    else
      retry_ms = window
    end
  end
  local basis = tokens
  if allowed == 1 then
    basis = new_tokens
  end
  local reset_ms = 0
  if rate > 0 and basis < capacity then
    reset_ms = math.ceil((capacity - math.max(basis, 0)) / rate)
  end
  local remaining = basis
  if remaining < 0 then
    remaining = 0
  end
  return allowed, math.floor(remaining), reset_ms, retry_ms, new_tokens
end

local function save(key, capacity, new_tokens)
  if capacity <= 0 then
    return
  end
  redis.call('HSET', key, 'tokens', new_tokens, 'updated_ms', now)
  redis.call('PEXPIRE', key, math.max(window * 2, 60000))
end

local req_tokens, req_rate = refill(req_key, req_capacity)
local tok_tokens, tok_rate = refill(tok_key, tok_capacity)
local req_ok, req_rem, req_reset, req_retry, req_new =
  plan(req_tokens, req_rate, req_capacity, req_cost)
local tok_ok, tok_rem, tok_reset, tok_retry, tok_new =
  plan(tok_tokens, tok_rate, tok_capacity, tok_cost)

if req_ok == 1 and tok_ok == 1 then
  save(req_key, req_capacity, req_new)
  save(tok_key, tok_capacity, tok_new)
  return {1, 0, req_rem, tok_rem, req_reset, tok_reset, 0}
end

local reason = 0
if req_ok == 0 then
  reason = 1
end
if tok_ok == 0 then
  if reason == 1 then
    reason = 3
  else
    reason = 2
  end
end
local retry_ms = req_retry
if tok_retry > retry_ms then
  retry_ms = tok_retry
end

-- Nothing was written. Report the buckets as they still are, not as a hypothetical spend.
local function show(level, rate, capacity)
  if capacity <= 0 then
    return -1, 0
  end
  local shown = level
  if shown < 0 then
    shown = 0
  end
  local reset_ms = 0
  if rate > 0 and shown < capacity then
    reset_ms = math.ceil((capacity - shown) / rate)
  end
  return math.floor(shown), reset_ms
end

local req_show, req_reset_now = show(req_tokens, req_rate, req_capacity)
local tok_show, tok_reset_now = show(tok_tokens, tok_rate, tok_capacity)
return {0, reason, req_show, tok_show, req_reset_now, tok_reset_now, retry_ms}
"""

_ADJUST = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local delta = tonumber(ARGV[3])
local window = tonumber(ARGV[4])

if capacity <= 0 then
  return {0, 0}
end

local raw = redis.call('HGET', key, 'tokens')
local updated = tonumber(redis.call('HGET', key, 'updated_ms') or '0')
local tokens
if not raw then
  tokens = capacity
else
  tokens = tonumber(raw)
end
local elapsed = math.max(0, now - (updated or now))
local rate = capacity / window
tokens = math.min(capacity, tokens + elapsed * rate)
tokens = tokens - delta
if tokens > capacity then
  tokens = capacity
end
redis.call('HSET', key, 'tokens', tokens, 'updated_ms', now)
redis.call('PEXPIRE', key, math.max(window * 2, 60000))
local shown = tokens
if shown < 0 then
  shown = 0
end
local reset_ms = 0
if rate > 0 and tokens < capacity then
  reset_ms = math.ceil((capacity - tokens) / rate)
end
return {math.floor(shown), reset_ms}
"""


def format_reset(milliseconds: int) -> str:
    """OpenAI-style delay: ``150ms``, ``2s``, ``1m5s``."""
    if milliseconds <= 0:
        return "0s"
    if milliseconds < 1000:
        return f"{milliseconds}ms"
    seconds, millis = divmod(milliseconds, 1000)
    if seconds < 60:
        if millis == 0:
            return f"{seconds}s"
        return f"{seconds}s{millis}ms"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m{seconds}s"


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    reason: int
    limit_requests: int
    limit_tokens: int
    remaining_requests: int
    remaining_tokens: int
    reset_requests_ms: int
    reset_tokens_ms: int
    retry_after_ms: int

    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.limit_requests > 0:
            headers["x-ratelimit-limit-requests"] = str(self.limit_requests)
            headers["x-ratelimit-remaining-requests"] = str(max(0, self.remaining_requests))
            headers["x-ratelimit-reset-requests"] = format_reset(self.reset_requests_ms)
        if self.limit_tokens > 0:
            headers["x-ratelimit-limit-tokens"] = str(self.limit_tokens)
            headers["x-ratelimit-remaining-tokens"] = str(max(0, self.remaining_tokens))
            headers["x-ratelimit-reset-tokens"] = format_reset(self.reset_tokens_ms)
        return headers

    def with_tokens(self, remaining: int, reset_ms: int) -> LimitDecision:
        return replace(self, remaining_tokens=remaining, reset_tokens_ms=reset_ms)

    def as_error(self) -> RateLimitError:
        if self.reason == 2:
            message = f"Rate limit reached for tokens per minute. Limit: {self.limit_tokens}."
        elif self.reason == 3:
            message = (
                "Rate limit reached for requests and tokens per minute. "
                f"Limits: {self.limit_requests} requests, {self.limit_tokens} tokens."
            )
        else:
            message = f"Rate limit reached for requests per minute. Limit: {self.limit_requests}."
        retry_after = max(1, math.ceil(self.retry_after_ms / 1000)) if self.retry_after_ms else 1
        headers = self.headers()
        headers["retry-after"] = str(retry_after)
        # The gateway itself is enforcing this. Retrying the same call immediately
        # will fail again, so the pipeline must not treat it as an upstream 429.
        return RateLimitError(
            message,
            retry_after=float(retry_after),
            response_headers=headers,
            retryable=False,
        )


def _numbers(raw: object, count: int) -> list[int]:
    if not isinstance(raw, (list, tuple)) or len(raw) < count:
        raise ProviderError(
            "Rate limiter returned an unexpected result",
            status_code=503,
            code="rate_limiter_unavailable",
            retryable=False,
        )
    return [int(float(item)) for item in raw[:count]]


class TokenBucketLimiter:
    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    async def acquire(
        self,
        *,
        key_id: str,
        requests_per_minute: int,
        tokens_per_minute: int,
        token_cost: int,
        now_ms: int,
    ) -> LimitDecision:
        try:
            raw: Any = await self._redis.eval(
                _ACQUIRE,
                2,
                f"llmgate:rl:{key_id}:requests",
                f"llmgate:rl:{key_id}:tokens",
                str(now_ms),
                str(requests_per_minute),
                "1",
                str(tokens_per_minute),
                str(max(0, token_cost)),
                str(_WINDOW_MS),
            )
        except Exception as exc:
            raise _unavailable() from exc
        allowed, reason, req_rem, tok_rem, req_reset, tok_reset, retry_ms = _numbers(raw, 7)
        return LimitDecision(
            allowed=allowed == 1,
            reason=reason,
            limit_requests=requests_per_minute,
            limit_tokens=tokens_per_minute,
            remaining_requests=req_rem,
            remaining_tokens=tok_rem,
            reset_requests_ms=req_reset,
            reset_tokens_ms=tok_reset,
            retry_after_ms=retry_ms,
        )

    async def adjust(
        self,
        *,
        key_id: str,
        tokens_per_minute: int,
        delta: int,
        now_ms: int,
    ) -> tuple[int, int]:
        """Add ``delta`` token consumption (negative refunds). Returns remaining, reset ms."""
        if delta == 0 or tokens_per_minute <= 0:
            return 0, 0
        try:
            raw: Any = await self._redis.eval(
                _ADJUST,
                1,
                f"llmgate:rl:{key_id}:tokens",
                str(now_ms),
                str(tokens_per_minute),
                str(delta),
                str(_WINDOW_MS),
            )
        except Exception as exc:
            raise _unavailable() from exc
        remaining, reset_ms = _numbers(raw, 2)
        return remaining, reset_ms


def _unavailable() -> ProviderError:
    return ProviderError(
        "Rate limiter is unavailable",
        status_code=503,
        code="rate_limiter_unavailable",
        retryable=False,
    )
