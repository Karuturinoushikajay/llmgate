"""Per-request upstream timeouts.

The configured timeout is the ceiling. A caller may send ``x-llmgate-timeout``
to shorten that call. They cannot extend it: a client must not be able to pin
a worker longer than the operator allowed.
"""

from __future__ import annotations

import httpx

from llmgate.config import Settings
from llmgate.core.errors import InvalidRequestError


def build_upstream_timeout(settings: Settings, header: str | None) -> httpx.Timeout:
    read = settings.upstream_timeout_seconds
    if header is not None and header.strip():
        try:
            requested = float(header.strip())
        except ValueError as exc:
            raise InvalidRequestError(
                "x-llmgate-timeout must be a number of seconds",
                param="x-llmgate-timeout",
            ) from exc
        if requested <= 0:
            raise InvalidRequestError(
                "x-llmgate-timeout must be greater than 0",
                param="x-llmgate-timeout",
            )
        read = min(requested, settings.upstream_timeout_seconds)
    connect = settings.upstream_connect_timeout_seconds
    return httpx.Timeout(
        connect=connect,
        read=read,
        write=min(30.0, read),
        pool=connect,
    )
