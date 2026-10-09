"""The chaos demo is deterministic. These counts are the ones quoted in the design note."""

from __future__ import annotations

import asyncio

from scripts.reliability_demo import collect, render

EXPECTED = """\
seed=7 trials=2000 primary_failure_rate=0.5 fallback_failure_rate=0.1 attempts=3
no_retry_no_fallback: 970/2000 (48.50%) primary_calls=2000 fallback_calls=0
retries_only: 1736/2000 (86.80%) primary_calls=3560 fallback_calls=0
retries_and_fallback: 2000/2000 (100.00%) primary_calls=3560 fallback_calls=290
breaker_without_fallback: 0/6 (0.00%) primary_calls=3 fallback_calls=0
breaker_with_fallback: 6/6 (100.00%) primary_calls=3 fallback_calls=6\
"""


def test_reliability_demo_counts() -> None:
    assert render(asyncio.run(collect())) == EXPECTED
