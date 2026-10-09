# LLMGate design notes (milestone 1)

These are the decisions worth being able to explain in an interview. Later milestones should extend the seams named here rather than rewriting the HTTP layer.

## Why the API is OpenAI-compatible

Most application code and every major agent framework already speaks `POST /v1/chat/completions`. If LLMGate implements that contract, a caller switches gateways by changing `base_url` and the API key. No new SDK, no new retry helper, no new streaming parser.

The cost is that we inherit OpenAI's shape even when the upstream API is different. That cost is paid once, inside an adapter, instead of once per application. Responses and errors are normalized back to OpenAI's schema so clients can branch on `finish_reason`, `usage`, and `error.type` without knowing which provider ran the request.

We deliberately support a subset, not a bug-for-bug clone. `n` must be 1. Tool calls are forwarded only to OpenAI. Unknown JSON fields are ignored so a newer SDK does not get a 422 for a field we do not route yet.

## Why adapters, and why a pipeline in front of them

Each provider module owns three translations:

1. OpenAI messages to the vendor payload (Anthropic system prompt and alternating roles, Gemini `user`/`model` turns, Ollama options).
2. Vendor streaming events to OpenAI chunks (`content_block_delta`, Gemini SSE, Ollama NDJSON).
3. Vendor token counts to `prompt_tokens` / `completion_tokens` / `total_tokens`.

The HTTP route never mentions those payloads. It calls `ChatPipeline.complete` or `ChatPipeline.open_stream`. Milestone 2 moved retries, fallback, circuit breakers, and rate limiting into that class; the route copies the headers the pipeline returns. Semantic cache, complexity routing, and guardrails should wrap the same class. Adding a provider is a new class plus a registry entry, not a change to the route. See [002-reliability.md](002-reliability.md).

Model names resolve in two steps. The YAML registry is for stable aliases (`claude-3-5-sonnet` to a dated upstream id). A `provider/model` prefix is an explicit override so operators can call a model that is not in the file, which matters while the registry is still small.

Provider credentials are process configuration. The request body cannot select an API key. A stolen gateway key can spend quota; it cannot extract the upstream secret.

## Why fully async

A chat call holds a connection for seconds, and a stream holds it for the whole generation. A synchronous worker would sit idle on network IO the entire time. FastAPI plus `httpx.AsyncClient` lets one process keep many upstream streams open. The shared client is created in the lifespan and closed on shutdown, with connect/read timeouts so a stuck provider cannot pin a request forever.

Streaming stays on a raw ASGI middleware instead of `BaseHTTPMiddleware`, which buffers or breaks response bodies. The middleware returns only after the body is sent, so the access log's latency includes the stream. Token counts are written onto `request.state` (the dict stored on the ASGI scope). A context variable set inside the streaming generator does not propagate back to that middleware under Uvicorn, because the body runs in a copied context; mutating the scope dict does.

Upstream HTTP failures that happen before the first token are JSON error responses. Failures after headers are sent become an SSE error object followed by `data: [DONE]`, because the status code can no longer change. That split is why the route primes the async generator before constructing `StreamingResponse`.

## Why gateway keys are hashed

A gateway key is a bearer token. Storing it in Postgres would make a database backup equivalent to every credential. We store HMAC-SHA256(key, pepper):

- The raw key is shown once, at creation, then discarded.
- A short prefix (`sk-lg-……`) is stored so operators can tell keys apart in `llmgate keys list` without the secret.
- The pepper lives in the environment. A dump of the `api_keys` table is not reversible without it, and the keys are high-entropy so the HMAC is not a password hash that needs bcrypt. Bcrypt would also be awkward here: authentication is a lookup, and an indexed HMAC is an equality query. A slow hash would force a scan.
- Lookup hashes the presented token and selects on `key_hash`. Revoked keys fail the same way unknown keys do, so the error does not reveal whether a key ever existed.
- Comparison of the master key uses SHA-256 digests with `hmac.compare_digest`, so the check does not leak the master key's length.

`last_used_at` is updated on successful auth. That is operational metadata, not an audit log. Milestone 5 adds the append-only request record. This milestone only puts the fields that record will need onto the JSON access line: request id, key id, model, provider, latency, and token counts. Prompts are not logged. Logging prompts would store customer content in a place that is easy to ship to a log aggregator and hard to delete.

Upstream authentication failures are returned as 502, not 401. A 401 means the caller's gateway key is wrong. A bad `OPENAI_API_KEY` is our misconfiguration, and reporting it as 401 would send clients rotating a key that is fine.

## Redis

Redis is connected during startup and `GET /healthz` pings it, so Compose fails closed if Redis is down. Milestone 2 stores circuit-breaker hashes and token buckets on that client (`llmgate:breaker:{provider}`, `llmgate:rl:{key_id}:requests`, `llmgate:rl:{key_id}:tokens`). Milestone 3's vector cache should use the same client rather than creating another one.

Schema changes go through Alembic. The process runs `upgrade head` on startup so `docker compose up` is enough. Tests use the same migration path against SQLite; CI also runs it against Postgres.

## Non-goals for milestone 1

Milestone 1 shipped no retries, no fallback, no cache, no budgets, no PII redaction, and no dashboard. Those are separate products. Shipping them half-finished inside an adapter would make the translation layer impossible to reason about.

Milestone 2 added retries, fallback, per-provider breakers, and per-key quotas. Still absent, and worth saying in an interview: no request log table, no semantic cache, no budget ledger, and a single shared `httpx` client per process. The limiter and the breaker are already shared across processes through Redis.
