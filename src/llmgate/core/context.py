"""Per-request values the access log reads after the handler finishes."""

from __future__ import annotations

from contextvars import ContextVar

from llmgate.core.schemas import Usage

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
usage_var: ContextVar[Usage | None] = ContextVar("usage", default=None)
model_var: ContextVar[str | None] = ContextVar("model", default=None)
provider_var: ContextVar[str | None] = ContextVar("provider", default=None)
stream_var: ContextVar[bool] = ContextVar("stream", default=False)
api_key_id_var: ContextVar[str | None] = ContextVar("api_key_id", default=None)
error_var: ContextVar[str | None] = ContextVar("error", default=None)
cache_var: ContextVar[str] = ContextVar("cache", default="bypass")
route_var: ContextVar[str] = ContextVar("route", default="bypass")
team_id_var: ContextVar[str | None] = ContextVar("team_id", default=None)
team_budget_var: ContextVar[int | None] = ContextVar("team_budget", default=None)
