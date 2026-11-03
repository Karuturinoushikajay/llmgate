# LLMGate design notes (milestone 3)

Milestone 2 left the provider call inside `ChatPipeline`, after the route has already spent one token from the caller's bucket. The semantic cache wraps that call. A hit never reaches a provider. A miss stores the completion that the caller actually received.

## Why a hit still costs a token

The bucket from milestone 2 is the quota. If a cached response were free, a client could loop on one prompt and spend nothing, which is the opposite of what the bucket is for. The cache saves provider latency and provider money. It does not save gateway quota.

## What is safe to reuse

Only a request with `temperature` set, and at or below `LLMGATE_CACHE_MAX_TEMPERATURE`, is eligible. The default ceiling is `0`. An omitted temperature is not cached: OpenAI's default is not deterministic, and a later identical call would not be the same sample.

Tool calls, tool messages, and `n` other than 1 are not cached. Those responses depend on the client continuing a conversation the cache cannot see.

The embedding is the message text: each role, then its text parts, one message per line. Images are omitted. The raw JSON is not embedded, so key order and unknown fields do not change the vector. The vector is stored against the public model the caller named, so `gpt-4o-mini` and `gpt-4o` do not share an answer. The stored document is the OpenAI-shaped completion, and the response `model` field stays the id the caller sent.

## Why Redis does the similarity search

Cosine similarity runs in Redis with a KNN query, threshold `0.92`, and a 10 minute TTL. `redis:7-alpine` cannot do that. Compose uses `redis/redis-stack-server:7.4.0-v3` so `FT.SEARCH` is available. The token bucket and the circuit breaker keep using the same Redis URL.

`FakeAsyncRedis` cannot run RediSearch. Tests use an in-memory index with the same cosine rule. Production uses the Redis index. A missing index is a miss, not an error: nothing has been stored yet.

Embeddings are OpenAI `text-embedding-3-small`, sent with the gateway's `httpx` client and `OPENAI_API_KEY`. A missing key, a failed embedding call, or a failed cache read skips the cache and continues to the provider. The cache is an optimization, like the circuit breaker. The rate limiter stays fail-closed. A failed write after a successful provider call is logged and the caller still receives that completion.

A stream that hits is replayed as SSE and still ends with `data: [DONE]`. A stream is stored only after every chunk has been produced without an error. The access line gains a `cache` field (`hit`, `miss`, or `bypass`). The response carries `X-LLMGate-Cache` with the same value. The hit log records the model and the score. It does not record the prompt.

## The load baseline

`scripts/load_baseline.py` sends concurrent chat requests at a gateway that is already running and prints the count, the error rate, and p50, p95, and p99 latency. `make load` runs it. It is a manual measurement, not a CI gate. Pytest does not call a provider to produce the baseline.

## What this milestone still does not do

Budgets, PII masking, and the injection denylist are milestone 4. See [004-guardrails.md](004-guardrails.md). The dashboard stays a later milestone. The cache does not store tool traffic, and it does not try to cache a completion that the provider refused.
