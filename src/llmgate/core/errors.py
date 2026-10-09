"""Errors rendered as OpenAI-style ``{"error": {...}}`` bodies."""

from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_type: str,
        param: str | None = None,
        code: str | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.param = param
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.response_headers = dict(response_headers or {})

    def to_body(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }


class AuthenticationError(GatewayError):
    def __init__(
        self,
        message: str = "Incorrect API key provided",
        *,
        code: str = "invalid_api_key",
    ) -> None:
        super().__init__(
            message,
            status_code=401,
            error_type="authentication_error",
            code=code,
        )


class InvalidRequestError(GatewayError):
    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=400,
            error_type="invalid_request_error",
            param=param,
            code=code,
        )


class ModelNotFoundError(GatewayError):
    def __init__(self, model: str) -> None:
        super().__init__(
            f"The model `{model}` does not exist",
            status_code=404,
            error_type="invalid_request_error",
            param="model",
            code="model_not_found",
        )


class NotFoundError(GatewayError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            status_code=404,
            error_type="invalid_request_error",
            code="not_found",
        )


class RateLimitError(GatewayError):
    def __init__(
        self,
        message: str = "Rate limit exceeded",
        *,
        retry_after: float | None = None,
        response_headers: dict[str, str] | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(
            message,
            status_code=429,
            error_type="rate_limit_error",
            code="rate_limit_exceeded",
            retryable=retryable,
            retry_after=retry_after,
            response_headers=response_headers,
        )


class ProviderError(GatewayError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        code: str = "provider_error",
        retryable: bool | None = None,
    ) -> None:
        error_type = "timeout" if status_code == 504 else "server_error"
        if retryable is None:
            retryable = status_code >= 500
        super().__init__(
            message,
            status_code=status_code,
            error_type=error_type,
            code=code,
            retryable=retryable,
        )


class CircuitOpenError(ProviderError):
    """The provider is skipped until the breaker cools down. Try the next fallback."""

    def __init__(self, provider: str) -> None:
        super().__init__(
            f"Circuit breaker open for provider '{provider}'",
            status_code=503,
            code="circuit_open",
            retryable=False,
        )
        self.provider = provider


class ProviderNotConfiguredError(GatewayError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            f"Provider '{provider}' is not configured",
            status_code=503,
            error_type="server_error",
            code="provider_not_configured",
        )
