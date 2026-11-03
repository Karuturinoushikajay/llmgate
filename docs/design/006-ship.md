# LLMGate design notes (milestone 6)

Milestone 5 added the request table, the audit trail, Prometheus, Grafana, and the admin UI. This milestone is how you run that stack, how CI proves it, and how you explain the seams in an interview.

## Why Compose is still the default

A single `docker compose up --build` starts Postgres, Redis Stack, the gateway, Prometheus, Grafana, and the dashboard. That matches how milestones 1 through 5 were developed: one process, shared Redis, Alembic on startup. Production can swap the Compose files for a managed database and a container orchestrator without changing the HTTP contract. The deploy note documents the env vars that must change before the process leaves a laptop, and a minimal Kubernetes Deployment that mounts the same image.

## Why CI hardens now

Earlier milestones already ran ruff, mypy, and pytest against SQLite plus FakeRedis, and one Postgres marker for migrations. CI now also starts Redis (for the budget and bucket paths that need a real client in integration tests when marked), builds the gateway image, and builds the dashboard. A failing image build means the Compose path is broken even if unit tests pass. The load baseline stays a manual `make load`; CI checks the percentile helper only.

## Why a benchmark report is checked in

`docs/benchmark.md` is a template filled from `make load` against a warm gateway. It records request count, error rate, p50/p95/p99, and cache hit count. The numbers on a laptop are not a product claim. They are a baseline so a later change can be compared with the same script and the same flags.

## Why the README is the interview script

The root README now points at every design note in order. The architecture note names the pipeline seam. Reliability, cache, guardrails, and observability each explain one layer. This ship note explains how the box runs. An interviewer who asks "why OpenAI-compatible" or "why Redis for the bucket" should be answered from those pages, not from a slide deck that drifts from the code.
