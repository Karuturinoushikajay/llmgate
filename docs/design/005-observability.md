# LLMGate design notes (milestone 5)

Milestone 4 left cost, the request table, Prometheus, and the dashboard undone. This milestone records what already happened on the access line, prices it, exposes the same counters to Prometheus, and puts a small admin UI in front of the master-key APIs. Deployment and the interview README are milestone 6.

## Why a table as well as a log line

The JSON access line is for operators watching a process. It is hard to sum by team, hard to keep after log rotation, and easy to ship to a place that cannot delete a prompt. The `request_logs` table stores the same fields the access line already has: request id, key id, team id, model, provider, route, cache outcome, status, latency, and token counts. Prompts are still not stored. Nothing in this table is updated after insert. A later correction is a new audit row, not a rewrite.

Estimated cost is dollars from `config/pricing.yaml`: prompt and completion rates per million tokens for the public model that served the call. A missing price is a null cost, not a zero, so a new model does not look free by accident. Cache hits still record the tokens that were returned; the dollar cost for a hit is zero because the provider was not billed.

## Why an audit trail is separate

Request logs answer "what did callers spend." Audit events answer "who changed the gateway." Creating a key, revoking a key, creating a team, and creating a dashboard user each append one row. The actor is the master key or a dashboard user id. The payload is a small JSON object without secrets. There is no update path.

## Why Prometheus and Grafana sit beside the process

The access line and the table are already enough for a human. Prometheus is for alerts and graphs that do not need SQL. Counters and histograms live in process memory and are scraped at `GET /metrics`. Grafana is Compose-only: a provisioned datasource and one dashboard that plots request rate, latency, tokens, and cache hits. The gateway does not embed Grafana. Turning the metrics stack off does not change chat behavior.

## Why the dashboard is a separate Next.js app

The OpenAI-compatible API stays the product. The dashboard is an operator tool that calls `/admin` with a bearer token. Operators are rows with a role of `admin` or `viewer`. Admins can mutate keys and teams. Viewers can read keys, teams, usage, logs, audit events, and the routing map from the registry. The master key still works for scripts and the CLI. RBAC here is two roles, not a general IAM system. Routing rules are shown from the loaded registry; editing `models.yaml` stays a deploy-time change.

## What this milestone still does not do

No multi-region failover, no SSO, no editable live routing without a restart, and no dollar budgets (token budgets stay from milestone 4). Milestone 6 packages the stack and writes the interview README.
