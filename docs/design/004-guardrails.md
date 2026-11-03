# LLMGate design notes (milestone 4)

Milestone 3 left the provider call and the cache inside `ChatPipeline`, after the route has already spent one token from the caller's bucket. This milestone adds the checks that belong in that same pipeline: a denylist, PII masking, a complexity route, and a monthly team budget. The HTTP body stays OpenAI-shaped. The dashboard, RBAC, the request log, and Prometheus stay milestone 5.

## Why the route stays thin

The route still authenticates and runs the token bucket. A blocked prompt still spends one bucket token, because the request happened. The pipeline then scans, masks, routes, and, only if a provider call is about to happen, checks the team budget. A cache hit never reaches that check.

## What is rejected, and what is rewritten

The denylist runs on the original user and tool text. System and developer messages are the operator's. A match is `400` with `code = content_policy` on `messages`, before the cache, the budget, or a provider. The error and the log carry a rule id (`ignore_previous`, `disregard_system`, `reveal_system_prompt`, `jailbreak`). They do not carry the prompt or the matched text. The list is a cheap filter. It is not a claim that prompt injection is solved.

PII masking runs next, on a copy of the request. Emails, US phone numbers, SSNs (`###-##-####`), and 13–19 digit card numbers that pass Luhn become `[EMAIL]`, `[PHONE]`, `[SSN]`, and `[CARD]`. Images are left alone. That masked copy is what gets embedded and what the provider sees, so the embedding call does not receive the raw value. Matches are not logged. Nothing puts the original value back into the response.

## Why only `auto` is routed

A caller who names `gpt-4o` or `openai/gpt-4o` asked for that model. Rewriting it would spend a different quota. The public id `auto` is the opt-in. After masking, the text is simple when it is at most `LLMGATE_ROUTE_SIMPLE_CHARS` (default 280), the request has at most two messages, and there is no code fence. Otherwise it is complex. Simple resolves to `gpt-4o-mini`, complex to `gpt-4o`, and that target's existing fallbacks still apply. There is no extra model call.

The response `model` field stays `auto`. Clients keep parsing one name. The access line's `model` and `provider` stay the candidate that served the request. `X-LLMGate-Route` is the chosen public id, or `bypass` when the caller named a model. The cache key stays the caller's id, which for this path is `auto`. Similar text routes the same way, so a hit is the same tier.

## Why the budget is a monthly counter

A team is a row with an optional monthly token cap. A key with no team, or a team with no cap, is unlimited, so keys created before this milestone keep working. The counter is Redis `llmgate:budget:{team_id}:{YYYY-MM}`. The commands are `GET` and `INCRBY`, not `EVAL`, so tests can use `FakeAsyncRedis`.

The check runs only when a provider call is about to happen. A cache hit does not add tokens: the provider was not called. A successful completion, or a stream that finishes without an error, adds `usage.total_tokens`. A missing usage adds nothing and logs `budget.usage_missing`. One in-flight call can cross the cap. The next call is `429` with `code = budget_exceeded` and `Retry-After` set to the seconds until the next UTC month. That is a different code from the per-key bucket, which stays `rate_limit_exceeded`.

If Redis cannot read the counter for a team that has a cap, the call fails closed with `503` and `code = budget_unavailable`. A gateway that cannot enforce the cap must not spend provider money. A key with no cap does not touch the budget key. The rate limiter stays fail-closed as well.

## What this milestone still does not do

Dollar prices, the request-log table, Prometheus, Grafana, and the dashboard are milestone 5. See [005-observability.md](005-observability.md). The denylist is not a classifier, and the mask is not a vault.
