"""GET /v1/models — the configured registry, not every upstream catalog."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from llmgate.api.deps import ApiKeyContext, get_gateway, require_api_key
from llmgate.core.schemas import ModelCard, ModelList

router = APIRouter(tags=["models"])


@router.get("/v1/models")
async def list_models(
    request: Request,
    _: ApiKeyContext = Depends(require_api_key),
) -> ModelList:
    gateway = get_gateway(request)
    return ModelList(
        data=[
            ModelCard(id=entry.id, created=gateway.registry.loaded_at, owned_by=entry.provider)
            for entry in gateway.registry.list_models()
        ]
    )
