# Reliability and traffic control (milestone 2)

Milestone 1 left `ChatPipeline` as a resolve-then-call seam. This milestone puts retries, fallback, circuit breaking, and rate limiting inside that class. The HTTP route still does not know which provider will answer. It calls `pipeline.complete` or `pipeline.open_stream` and copies the headers those methods return.

## What a request does

1. Estimate tokens (`chars // 4` for the prompt, plus `max_tokens` or `LLMGATE_OUTPUT_TOKEN_RESERVATION`, default 256) and acquire one request plus that many tokens from the key's buckets.
2. Walk the model's target chain. The primary is the registry entry. Fallbacks are the ordered `fallbacks` list in `config/models.yaml`. A `provider/model` name is a one-element chain: the caller already picked the provider.
3. For each target, ask the provider's circuit breaker. An open breaker raises `circuit_open` and the pipeline moves to the next target without a network call.
4. Retry that target while the error is retryable and attempts remain. Streaming retries only until the first chunk is pulled from the upstream iterator. After that chunk exists, the status line is already 200 and a retry would duplicate output.
5. On success, record a breaker success and replace the token reservation with measured `usage.total_tokens` when the provider sent it. On failure before any response, refund the token reservation and keep the request charge: the client did send a request.
6. Put `x-llmgate-provider` and `x-llmgate-model` on the response. The JSON `model` field stays the name the client asked for, so an SDK that echoes the request model does not see a surprise id. The headers are how you tell which upstream actually served the call.

## Why retries look like this

Retryable means the same request might succeed if we wait: timeouts, connection errors, HTTP 429, and 5xx. A 4xx that describes the request (unknown model, bad schema, missing tools support) is not retried. Upstream 401 and 403 become a non-retryable 502: the gateway's provider credential is wrong, and retrying it just burns the attempt budget. The gateway's own 429 is also non-retryable. The client must back off; we must not call the provider at all.

Delay is equal jitter. After attempt `n` (1 = first failure) the exponential term is `base * 2^(n-1)`, capped at `max_delay`. The sleep is half of that term plus a random half. Equal jitter keeps a floor so we do not retry immediately, and it spreads clients that failed together so they do not stampede the upstream on the same millisecond. Full jitter (random in `[0, exponential]`) has a lower expected delay but a higher chance of an immediate retry, which is the wrong default against a provider that just returned 503.

`Retry-After` can only lengthen that wait. A short header does not cancel our backoff. The wait is then capped by `LLMGATE_RETRY_AFTER_CAP_SECONDS` (default 30). An upstream that sends `Retry-After: 3600` must not pin a worker for an hour.

Defaults: 3 attempts, 0.25s base, 8s cap. Tests set attempts to 1 and the base delay to 0 so the suite stays single-shot unless a test is specifically about retries.

## Why fallback is a chain, not a retry

A retry asks the same provider again. A fallback asks a different one because this provider is unlikely to succeed soon: retries exhausted, breaker open, provider not configured, or the model is missing on that vendor. A generic 400 does not fall back. Sending the same bad request to a second vendor usually produces a second 400 and hides the real error.

`gpt-4o` falls back to Anthropic Haiku, then Gemini Flash. `gpt-4o-mini` has no chain, so an upstream 429 on that alias is still a 429. Prefixed names have no chain on purpose: `openai/gpt-4o` means "this provider."

Tool calls are OpenAI-only. If the primary cannot represent tools, the pipeline raises immediately. A fallback that cannot represent them is skipped, and the original upstream error is what the client sees. Silently dropping tools would return a completion that ignored the caller's contract.

The breaker is acquired once per target per user request. Retries of that attempt do not re-acquire, so a half-open probe's retries are still the one probe. Success is recorded when `complete` returns, or when a stream yields its first chunk. A failure after the first chunk does not retry and does not re-open the breaker. The breaker measures "the provider started a response," which is the signal we can act on before bytes have been committed to the client. Mid-stream death is logged as a stream error and left for the next request to discover.

## Circuit breaker tradeoffs

State is one Redis hash per provider, `llmgate:breaker:{provider}`: `state`, `failures`, `opened_at`, `inflight`, `probe_at`.

- **Closed.** Consecutive retryable failures increment. A success deletes the key, which is a clean closed state and resets the streak. Threshold default is 5.
- **Open.** `allow` returns false until `now >= opened_at + cooldown` (default 30s). The pipeline skips the provider.
- **Half-open.** One probe is admitted (`LLMGATE_BREAKER_HALF_OPEN_MAX`, default 1). Success closes. Failure opens again for a full cooldown. If the probe's caller dies without recording an outcome, the next acquire after another cooldown replaces it (`probe_at + cooldown`), so a crashed gateway cannot leave the provider dark forever.

Only retryable non-429 failures count: timeouts, 5xx, connection errors. A 429 means the provider is up and asking us to slow down. Counting it would open the breaker during a quota incident and throw away a provider that is healthy. `model_not_found` and "provider not configured" are our routing problem, not the vendor's health.

The transition is one Lua script (`acquire`, `success`, `failure`) so two processes cannot both send the probe or both miss the threshold. The clock is the caller's `now_ms`, which makes the state machine testable without freezing Redis `TIME`. The cost is clock skew: if one instance's clock is ahead, it can probe early or count a cooldown as already elapsed. NTP keeps that small compared with a 30s cooldown. Redis `TIME` would remove the skew and make unit tests depend on a real server clock.

Redis errors fail open. The breaker logs a warning and allows the call. A Redis blip should not black-hole every provider. The limiter does the opposite (below).

## Why a token bucket

A fixed window allows `2N` requests at the boundary: `N` in the last millisecond of one minute and `N` in the first millisecond of the next. A sliding window log stores every request and is exact, but it is a lot of keys for a chat gateway that also counts tokens. A token bucket refills continuously at `capacity / 60s` and caps at `capacity`. A burst can spend the bucket, then traffic is smooth. That matches how providers bill and how clients expect `x-ratelimit-reset-*` to behave.

There are two buckets per API key, `llmgate:rl:{key_id}:requests` and `:tokens`. Requests per minute stop a tight loop. Tokens per minute stop one request with a huge prompt, and a loop of small ones. Either bucket at capacity `0` is unlimited. A key column of `NULL` inherits `LLMGATE_DEFAULT_REQUESTS_PER_MINUTE` (60) and `LLMGATE_DEFAULT_TOKENS_PER_MINUTE` (100000) at request time, so changing the env applies to existing keys. `0` on the key disables that bucket for that key. Admin `POST /admin/keys` and `llmgate keys create --requests-per-minute` / `--tokens-per-minute` set the columns.

The reservation is deliberate. TPM cannot wait for the provider to return usage, because by then the tokens are already spent upstream. We reserve an estimate, refund it if the call fails before a response, and adjust by `actual - reserved` when usage arrives. Missing usage keeps the reservation, which over-charges slightly and under-charges never. Streaming headers are snapshotted when `StreamingResponse` is built. `settle_stream` corrects the bucket after the body; it cannot rewrite headers that have already been sent.

A denied call returns 429 with an OpenAI error body, integer `Retry-After` of at least 1 second, and `x-ratelimit-limit-*`, `x-ratelimit-remaining-*`, `x-ratelimit-reset-*`. Reset strings follow OpenAI's short form (`150ms`, `2s`, `1s500ms`, `1m5s`). Unlimited buckets omit their headers. The 429 happens before any provider call.

## Why the bucket is a Lua script

The acquire refills both buckets, plans both spends, and writes both only if both allow. A token denial must not consume a request, and the headers on a denial report the pre-cost level that is still stored, not a hypothetical post-spend. Doing that with `WATCH`/`MULTI` is a compare-and-swap loop: under contention it retries, and two keys mean two watches and a window where one process refills while another commits. One `EVAL` is a single round trip and Redis runs it alone. Every gateway process shares the same buckets, which is the point of putting the limiter in Redis instead of in process memory.

We send the script with `EVAL` rather than `SCRIPT LOAD` + `EVALSHA`. `EVAL` re-parses a short script; the extra round trip of a cache miss is not worth a loader and a `NOSCRIPT` retry until this shows up in a profile. Production Redis has Lua built in. Tests use `fakeredis` with `lupa` so the same script runs without a server, and CI also points `LLMGATE_TEST_REDIS_URL` at a Redis 7 service and `FLUSHDB`s it per test.

Limiter Redis errors fail closed: 503 `rate_limiter_unavailable`, not retryable. Fail-open would let a Redis outage disable every quota. That is the opposite of the breaker, and both choices are intentional. The breaker is a performance optimization around a provider that is already failing. The limiter is the quota.

## Per-request timeouts

`LLMGATE_UPSTREAM_TIMEOUT_SECONDS` (default 60) is the read timeout for one attempt. Connect defaults to 10s, write to `min(30, read)`. The shared httpx client still has a longer ceiling (`LLMGATE_REQUEST_TIMEOUT_SECONDS`, 300) for anything that does not pass an explicit timeout. A caller may send `x-llmgate-timeout` to shorten the read timeout. A larger value is ignored; a non-positive or non-numeric value is 400. The attempt budget is per try, so three attempts can take about three timeouts plus backoff, bounded by the retry cap.

## Measured demo

`python scripts/reliability_demo.py` runs the real `ChatPipeline` against an in-process provider. No network and no credentials. Primary calls fail independently with probability 0.50, the fallback with 0.10, seed 7, 2000 trials, 3 attempts. Each scenario rebuilds `Random(seed)`, so a re-run prints the same counts. Delays are zero. The breaker threshold is 10,000 in the probability scenarios so it does not open. Rate limits are unlimited (`0` / `0`).

Independent failures give a theoretical success rate of `1 - 0.50^attempts` with retries only, and `1 - (0.50^attempts) * (0.10^attempts)` when the fallback also gets `attempts` tries. That second figure is about 99.9875%, not 98.75%. 98.75% would be one fallback attempt (`1 - 0.125 * 0.10`).

| Scenario | Successes | Rate | Primary calls | Fallback calls |
| --- | ---: | ---: | ---: | ---: |
| no retry, no fallback | 970/2000 | 48.50% | 2000 | 0 |
| retries only | 1736/2000 | 86.80% | 3560 | 0 |
| retries and fallback | 2000/2000 | 100.00% | 3560 | 290 |
| breaker, no fallback | 0/6 | 0.00% | 3 | 0 |
| breaker and fallback | 6/6 | 100.00% | 3 | 6 |

Theory for a fair coin at 2000 trials is 50% and 87.5%. The sample landed at 48.50% and 86.80%. Retries-only had 264 failures (`2000 - 1736`). Those are the trials that reach the fallback. About 10% of them need a second fallback try, and `264 + 26 = 290` fallback calls, which matches the table. 2000/2000 with both features is the sample, not a guarantee: about one trial in 8000 should still fail at these rates.

The breaker rows use a primary that always fails, threshold 3, one attempt, cooldown 3600s, six trials. Without a fallback the breaker opens on the third failure and the next three trials never call it (`primary_calls=3`). With a fallback those three trials are served by the second provider (`fallback_calls=6`: three while the primary is still being probed, three after it is open).

## What this milestone still does not do

No semantic cache, no budget ledger, no per-team limits, no hedge (sending the fallback before the primary finishes). Hedging would improve tail latency and double the bill. The chain here waits until the primary has failed. RPM is charged even when the breaker is already open, because the gateway still did the work of authenticating and rejecting. A cache, if milestone 3 adds one, should sit in this pipeline in front of the reserve-and-execute path so a hit does not spend TPM twice.
