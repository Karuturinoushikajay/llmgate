"""Per-provider circuit breaker whose state lives in Redis.

Closed: calls go through, consecutive failures are counted.
Open: calls are rejected until ``cooldown`` elapses.
Half-open: a bounded number of probes are allowed. One success closes the
breaker. A probe failure opens it again.

The transition runs in a Lua script so two gateway processes cannot both
send a probe, or both miss the failure threshold.
"""

from __future__ import annotations

from typing import Any

import structlog

from llmgate.core.redis_client import RedisClient

log = structlog.get_logger("llmgate.breaker")

_SCRIPT = """
local key = KEYS[1]
local op = ARGV[1]
local now = tonumber(ARGV[2])
local threshold = tonumber(ARGV[3])
local cooldown = tonumber(ARGV[4])
local half_open_max = tonumber(ARGV[5])
local ttl = math.max(cooldown * 2, 120000)

local function load()
  local state = redis.call('HGET', key, 'state')
  if not state then
    state = 'closed'
  end
  local failures = tonumber(redis.call('HGET', key, 'failures') or '0')
  local opened_at = tonumber(redis.call('HGET', key, 'opened_at') or '0')
  local inflight = tonumber(redis.call('HGET', key, 'inflight') or '0')
  local probe_at = tonumber(redis.call('HGET', key, 'probe_at') or '0')
  return state, failures, opened_at, inflight, probe_at
end

local function save(state, failures, opened_at, inflight, probe_at)
  redis.call(
    'HSET', key,
    'state', state,
    'failures', failures,
    'opened_at', opened_at,
    'inflight', inflight,
    'probe_at', probe_at
  )
  redis.call('PEXPIRE', key, ttl)
end

if op == 'success' then
  redis.call('DEL', key)
  return {1, 'closed'}
end

local state, failures, opened_at, inflight, probe_at = load()

if op == 'failure' then
  if state == 'half_open' or state == 'open' then
    save('open', threshold, now, 0, 0)
    return {1, 'open'}
  end
  failures = failures + 1
  if failures >= threshold then
    save('open', failures, now, 0, 0)
    return {1, 'open'}
  end
  save('closed', failures, 0, 0, 0)
  return {1, 'closed'}
end

-- acquire
if state == 'open' then
  if now < opened_at + cooldown then
    return {0, 'open'}
  end
  save('half_open', failures, opened_at, 1, now)
  return {1, 'half_open'}
end

if state == 'half_open' then
  if now >= probe_at + cooldown then
    save('half_open', failures, opened_at, 1, now)
    return {1, 'half_open'}
  end
  if inflight < half_open_max then
    save('half_open', failures, opened_at, inflight + 1, now)
    return {1, 'half_open'}
  end
  return {0, 'half_open'}
end

return {1, 'closed'}
"""


def _pair(raw: object) -> tuple[int, str]:
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        raise RuntimeError("circuit breaker script returned an unexpected value")
    return int(raw[0]), str(raw[1])


class CircuitBreaker:
    def __init__(
        self,
        redis: RedisClient,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        half_open_max: int,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")
        if half_open_max < 1:
            raise ValueError("half_open_max must be at least 1")
        self._redis = redis
        self.failure_threshold = failure_threshold
        self.cooldown_ms = int(cooldown_seconds * 1000)
        self.half_open_max = half_open_max

    async def allow(self, provider: str, *, now_ms: int) -> bool:
        try:
            allowed, _state = await self._eval("acquire", provider, now_ms)
        except Exception:
            # A Redis blip should not black-hole traffic. The next successful
            # read still sees whatever state was stored.
            log.warning("breaker.unavailable", provider=provider)
            return True
        return allowed == 1

    async def record_success(self, provider: str, *, now_ms: int) -> None:
        await self._record("success", provider, now_ms)

    async def record_failure(self, provider: str, *, now_ms: int) -> str:
        try:
            _allowed, state = await self._eval("failure", provider, now_ms)
        except Exception:
            log.warning("breaker.record_failed", provider=provider)
            return "unknown"
        if state == "open":
            log.info("breaker.open", provider=provider)
        return state

    async def _record(self, op: str, provider: str, now_ms: int) -> None:
        try:
            await self._eval(op, provider, now_ms)
        except Exception:
            log.warning("breaker.record_failed", provider=provider, op=op)

    async def _eval(self, op: str, provider: str, now_ms: int) -> tuple[int, str]:
        raw: Any = await self._redis.eval(
            _SCRIPT,
            1,
            f"llmgate:breaker:{provider}",
            op,
            str(now_ms),
            str(self.failure_threshold),
            str(self.cooldown_ms),
            str(self.half_open_max),
        )
        return _pair(raw)
