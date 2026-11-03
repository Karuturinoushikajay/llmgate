"""In-process Prometheus metrics. Scraped at GET /metrics."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUESTS = Counter(
    "llmgate_requests_total",
    "HTTP requests handled by the gateway",
    ["method", "path", "status"],
)
LATENCY = Histogram(
    "llmgate_request_latency_seconds",
    "Request latency including stream duration",
    ["path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
TOKENS = Counter(
    "llmgate_tokens_total",
    "Tokens reported on completed chat calls",
    ["model", "kind"],
)
CACHE = Counter(
    "llmgate_cache_total",
    "Semantic cache outcomes",
    ["outcome"],
)
COST = Counter(
    "llmgate_estimated_cost_usd_total",
    "Estimated USD spent on provider calls",
)


def observe_request(
    *,
    method: str,
    path: str,
    status_code: int,
    latency_seconds: float,
    model: str | None,
    cache: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    estimated_cost_usd: float | None,
) -> None:
    label_path = path if path.startswith("/v1") or path.startswith("/admin") else "other"
    REQUESTS.labels(method=method, path=label_path, status=str(status_code)).inc()
    LATENCY.labels(path=label_path).observe(latency_seconds)
    CACHE.labels(outcome=cache or "bypass").inc()
    if model and prompt_tokens:
        TOKENS.labels(model=model, kind="prompt").inc(prompt_tokens)
    if model and completion_tokens:
        TOKENS.labels(model=model, kind="completion").inc(completion_tokens)
    if estimated_cost_usd:
        COST.inc(estimated_cost_usd)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
