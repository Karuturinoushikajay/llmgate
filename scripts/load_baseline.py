"""Concurrent chat baseline against a gateway that is already running.

Pytest does not execute this script. It only checks ``percentile``.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import time

import httpx


def percentile(values: list[float], pct: float) -> float:
    """Linear percentile. ``pct`` is in the range 0 to 100."""
    if not values:
        raise ValueError("no latencies")
    if pct < 0 or pct > 100:
        raise ValueError("percentile out of range")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


async def _one(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    model: str,
) -> tuple[float, int, str]:
    started = time.perf_counter()
    response = await client.post(
        url,
        headers={"authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "temperature": 0,
            "messages": [{"role": "user", "content": "Say hello in one sentence."}],
        },
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    cache = response.headers.get("x-llmgate-cache", "")
    return elapsed_ms, response.status_code, cache


async def run(
    *,
    base_url: str,
    api_key: str,
    model: str,
    concurrency: int,
    requests: int,
) -> None:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    limits = httpx.Limits(max_connections=concurrency)
    async with httpx.AsyncClient(timeout=60.0, limits=limits) as client:
        latencies: list[float] = []
        errors = 0
        hits = 0
        pending = 0

        async def worker() -> None:
            nonlocal errors, hits, pending
            while True:
                if pending >= requests:
                    return
                pending += 1
                elapsed, status, cache = await _one(client, url, api_key, model)
                latencies.append(elapsed)
                if status >= 400:
                    errors += 1
                if cache == "hit":
                    hits += 1

        await asyncio.gather(*(worker() for _ in range(concurrency)))

    print(f"requests: {len(latencies)}")
    print(f"errors: {errors}")
    print(f"cache_hits: {hits}")
    print(f"p50_ms: {percentile(latencies, 50):.2f}")
    print(f"p95_ms: {percentile(latencies, 95):.2f}")
    print(f"p99_ms: {percentile(latencies, 99):.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="LLMGate load baseline")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--requests", type=int, default=20)
    args = parser.parse_args()
    asyncio.run(
        run(
            base_url=args.base_url,
            api_key=args.api_key,
            model=args.model,
            concurrency=args.concurrency,
            requests=args.requests,
        )
    )


if __name__ == "__main__":
    main()
