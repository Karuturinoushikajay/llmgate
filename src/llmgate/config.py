"""Runtime configuration loaded from the environment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_MASTER_KEY = "dev-master-key-change-me"
_DEFAULT_KEY_PEPPER = "dev-pepper-change-me"


class Settings(BaseSettings):
    """Process configuration.

    Gateway settings use ``LLMGATE_*`` names. Provider credentials use the
    vendor's conventional variable names so one ``.env`` serves local tools
    and the gateway. Credentials are never read from request bodies.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(
        default="postgresql+asyncpg://llmgate:llmgate@localhost:5432/llmgate",
        alias="LLMGATE_DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="LLMGATE_REDIS_URL")
    master_key: str = Field(default=_DEFAULT_MASTER_KEY, alias="LLMGATE_MASTER_KEY")
    key_pepper: str = Field(default=_DEFAULT_KEY_PEPPER, alias="LLMGATE_KEY_PEPPER")
    models_path: str = Field(default="config/models.yaml", alias="LLMGATE_MODELS_PATH")
    log_level: str = Field(default="INFO", alias="LLMGATE_LOG_LEVEL")
    alembic_ini: str = Field(default="alembic.ini", alias="LLMGATE_ALEMBIC_INI")
    request_timeout_seconds: float = Field(default=300.0, alias="LLMGATE_REQUEST_TIMEOUT_SECONDS")
    upstream_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        alias="LLMGATE_UPSTREAM_TIMEOUT_SECONDS",
    )
    upstream_connect_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        alias="LLMGATE_UPSTREAM_CONNECT_TIMEOUT_SECONDS",
    )
    retry_max_attempts: int = Field(default=3, ge=1, alias="LLMGATE_RETRY_MAX_ATTEMPTS")
    retry_base_delay_seconds: float = Field(
        default=0.25,
        ge=0,
        alias="LLMGATE_RETRY_BASE_DELAY_SECONDS",
    )
    retry_max_delay_seconds: float = Field(
        default=8.0,
        ge=0,
        alias="LLMGATE_RETRY_MAX_DELAY_SECONDS",
    )
    retry_after_cap_seconds: float = Field(
        default=30.0,
        ge=0,
        alias="LLMGATE_RETRY_AFTER_CAP_SECONDS",
    )
    breaker_failure_threshold: int = Field(
        default=5,
        ge=1,
        alias="LLMGATE_BREAKER_FAILURE_THRESHOLD",
    )
    breaker_cooldown_seconds: float = Field(
        default=30.0,
        ge=0,
        alias="LLMGATE_BREAKER_COOLDOWN_SECONDS",
    )
    breaker_half_open_max: int = Field(default=1, ge=1, alias="LLMGATE_BREAKER_HALF_OPEN_MAX")
    default_requests_per_minute: int = Field(
        default=60,
        ge=0,
        alias="LLMGATE_DEFAULT_REQUESTS_PER_MINUTE",
    )
    default_tokens_per_minute: int = Field(
        default=100_000,
        ge=0,
        alias="LLMGATE_DEFAULT_TOKENS_PER_MINUTE",
    )
    output_token_reservation: int = Field(
        default=256,
        ge=0,
        alias="LLMGATE_OUTPUT_TOKEN_RESERVATION",
    )

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_base_url: str = Field(default="https://api.anthropic.com", alias="ANTHROPIC_BASE_URL")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com",
        alias="GEMINI_BASE_URL",
    )
    ollama_base_url: str = Field(default="http://localhost:11434", alias="OLLAMA_BASE_URL")

    @property
    def using_default_secrets(self) -> bool:
        return self.master_key == _DEFAULT_MASTER_KEY or self.key_pepper == _DEFAULT_KEY_PEPPER


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def resolve_models_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(
        f"Model registry not found at {candidate}. "
        "Set LLMGATE_MODELS_PATH or run commands from the repository root."
    )
