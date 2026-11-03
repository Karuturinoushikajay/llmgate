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
    retry_max_attempts: int = Field(default=3, alias="LLMGATE_RETRY_MAX_ATTEMPTS", ge=1)
    retry_base_seconds: float = Field(default=0.2, alias="LLMGATE_RETRY_BASE_SECONDS", ge=0)
    retry_max_seconds: float = Field(default=2.0, alias="LLMGATE_RETRY_MAX_SECONDS", ge=0)
    cb_failure_threshold: int = Field(default=5, alias="LLMGATE_CB_FAILURE_THRESHOLD", ge=1)
    cb_recovery_seconds: float = Field(default=30.0, alias="LLMGATE_CB_RECOVERY_SECONDS", gt=0)
    rate_limit_burst: float = Field(default=60, alias="LLMGATE_RATE_LIMIT_BURST", ge=1)
    rate_limit_refill_per_second: float = Field(
        default=1.0,
        alias="LLMGATE_RATE_LIMIT_REFILL_PER_SECOND",
        gt=0,
    )
    cache_enabled: bool = Field(default=True, alias="LLMGATE_CACHE_ENABLED")
    cache_similarity: float = Field(default=0.92, alias="LLMGATE_CACHE_SIMILARITY", ge=0, le=1)
    cache_ttl_seconds: int = Field(default=600, alias="LLMGATE_CACHE_TTL_SECONDS", gt=0)
    cache_embedding_model: str = Field(
        default="text-embedding-3-small",
        alias="LLMGATE_CACHE_EMBEDDING_MODEL",
        min_length=1,
    )
    cache_max_temperature: float = Field(
        default=0,
        alias="LLMGATE_CACHE_MAX_TEMPERATURE",
        ge=0,
        le=2,
    )
    route_simple_chars: int = Field(default=280, alias="LLMGATE_ROUTE_SIMPLE_CHARS", ge=1)
    pii_masking: bool = Field(default=True, alias="LLMGATE_PII_MASKING")
    guardrails_enabled: bool = Field(default=True, alias="LLMGATE_GUARDRAILS_ENABLED")
    pricing_path: str = Field(default="config/pricing.yaml", alias="LLMGATE_PRICING_PATH")

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
