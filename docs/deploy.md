# Deploying LLMGate

## Compose (default)

From the repository root:

```bash
cp .env.example .env
# set LLMGATE_MASTER_KEY, LLMGATE_KEY_PEPPER, provider keys
docker compose up --build
```

Services:

| Service | Port | Role |
| --- | --- | --- |
| gateway | 8000 | OpenAI-compatible API + `/admin` + `/metrics` |
| postgres | internal | API keys, teams, request logs, audit |
| redis | internal | Redis Stack for bucket, breaker, cache, budgets |
| prometheus | 9090 | Scrapes `/metrics` |
| grafana | 3001 | Provisioned LLMGate dashboard (admin/admin) |
| dashboard | 3000 | Next.js operator UI |

Change secrets before the process leaves a laptop. The Compose defaults are for development only.

## Required environment

- `LLMGATE_DATABASE_URL`
- `LLMGATE_REDIS_URL`
- `LLMGATE_MASTER_KEY`
- `LLMGATE_KEY_PEPPER`
- Provider keys as needed (`OPENAI_API_KEY`, …)
- `LLMGATE_PRICING_PATH` (default `config/pricing.yaml`)
- Dashboard: `NEXT_PUBLIC_GATEWAY_URL` pointing at a URL the browser can reach

## Kubernetes sketch

Build and push the gateway image, then run one Deployment with the same env vars, a Service on 8000, and managed Postgres/Redis. Mount `config/models.yaml` and `config/pricing.yaml` as a ConfigMap. Run Alembic via the process startup path already used in Compose (`upgrade head` on boot). Scrape `/metrics` with your cluster Prometheus.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llmgate
spec:
  replicas: 2
  selector:
    matchLabels:
      app: llmgate
  template:
    metadata:
      labels:
        app: llmgate
    spec:
      containers:
        - name: gateway
          image: ghcr.io/example/llmgate:latest
          ports:
            - containerPort: 8000
          envFrom:
            - secretRef:
                name: llmgate-secrets
```

## Checklist before production

1. Rotate `LLMGATE_MASTER_KEY` and `LLMGATE_KEY_PEPPER`.
2. Use a managed Postgres with backups.
3. Point Redis at Redis Stack or another RediSearch-capable server if you keep the semantic cache.
4. Restrict `/admin` and the dashboard to a private network.
5. Confirm `/healthz` is green and `/metrics` scrapes.
