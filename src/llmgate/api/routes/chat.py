"""POST /v1/chat/completions."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from llmgate.api.deps import ApiKeyContext, get_gateway, require_api_key
from llmgate.api.sse import format_chunk, format_done, format_error, should_emit
from llmgate.core.context import model_var, provider_var, stream_var, usage_var
from llmgate.core.errors import GatewayError, ProviderError
from llmgate.core.schemas import ChatCompletionChunk, ChatCompletionRequest, Usage

router = APIRouter(tags=["chat"])


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    _api_key: ApiKeyContext = Depends(require_api_key),
) -> Response:
    """OpenAI chat completions. ``stream: true`` returns SSE ending in ``data: [DONE]``."""
    gateway = get_gateway(request)
    model_var.set(body.model)
    stream_var.set(body.stream)
    await gateway.limiter.consume(str(_api_key.id))

    if body.stream:
        return await _streaming_response(request, gateway.pipeline.stream(body), body)

    result = await gateway.pipeline.complete(body)
    if result.usage is not None:
        usage_var.set(result.usage)
    return JSONResponse(content=result.model_dump(mode="json", exclude_none=True))


async def _streaming_response(
    request: Request,
    iterator: AsyncGenerator[ChatCompletionChunk, None],
    body: ChatCompletionRequest,
) -> StreamingResponse:
    try:
        first = await anext(iterator)
    except StopAsyncIteration:
        first = None
    except BaseException:
        await iterator.aclose()
        raise

    async def generate() -> AsyncIterator[str]:
        # Usage is stored on request.state, not only a ContextVar. Uvicorn runs
        # the stream body in a copied context, so a ContextVar set inside this
        # generator is invisible to the access-log middleware.
        observed: Usage | None = None
        try:
            if first is not None:
                observed = _remember(request, first, observed)
                if should_emit(first, include_usage=body.include_usage):
                    yield format_chunk(first, include_usage=body.include_usage)
            async for chunk in iterator:
                observed = _remember(request, chunk, observed)
                if should_emit(chunk, include_usage=body.include_usage):
                    yield format_chunk(chunk, include_usage=body.include_usage)
            yield format_done()
        except GatewayError as exc:
            _remember_error(request, exc.message)
            yield format_error(exc)
            yield format_done()
        except httpx.HTTPError as exc:
            mapped = _transport_error(provider_var.get() or "provider", exc)
            _remember_error(request, mapped.message)
            yield format_error(mapped)
            yield format_done()
        finally:
            await iterator.aclose()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _remember(request: Request, chunk: ChatCompletionChunk, current: Usage | None) -> Usage | None:
    if chunk.usage is None:
        return current
    request.state.usage = chunk.usage
    usage_var.set(chunk.usage)
    return chunk.usage


def _remember_error(request: Request, message: str) -> None:
    request.state.stream_error = message


def _transport_error(provider_name: str, exc: httpx.HTTPError) -> ProviderError:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(f"Timed out waiting for {provider_name}", status_code=504)
    return ProviderError(f"Failed to reach {provider_name}")
