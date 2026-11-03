# Load baseline report

Fill this from `make load` against a warm gateway. Numbers on a laptop are a comparison baseline, not a product claim.

## Environment

| Field | Value |
| --- | --- |
| Date | YYYY-MM-DD |
| Host | |
| Gateway version | |
| Compose / host run | |
| Model | gpt-4o-mini |
| Temperature | 0 |
| Concurrency | 4 |
| Requests | 20 |
| Provider | real / mock |

## Command

```bash
make load BASE_URL=http://127.0.0.1:8000 API_KEY=sk-lg-... MODEL=gpt-4o-mini CONCURRENCY=4 REQUESTS=20
```

## Results

| Metric | Value |
| --- | --- |
| requests | |
| errors | |
| cache_hits | |
| p50_ms | |
| p95_ms | |
| p99_ms | |

## Notes

Record whether Redis Stack was up, whether the semantic cache was warm, and whether provider keys pointed at a real upstream or a mock.
