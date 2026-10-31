from __future__ import annotations

import pytest
from scripts.load_baseline import percentile


def test_percentile_interpolates_a_fixed_sample() -> None:
    values = [float(item) for item in range(1, 101)]
    assert percentile(values, 0) == 1
    assert percentile(values, 50) == 50.5
    assert percentile(values, 100) == 100
    assert percentile([8.0], 95) == 8


def test_percentile_rejects_an_empty_sample() -> None:
    with pytest.raises(ValueError, match="no latencies"):
        percentile([], 50)
