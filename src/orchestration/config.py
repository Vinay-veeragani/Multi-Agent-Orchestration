"""Typed application configuration.

All settings are read from the environment (prefix ``ORCH_``) or a local ``.env``.
Secrets are held as :class:`~pydantic.SecretStr` so that accidental interpolation
into a log line or a traceback renders ``**********`` instead of the value.
"""

from __future__ import annotations

import functools
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    CI = "ci"
    PRODUCTION = "production"


class ProviderName(StrEnum):
    MOCK = "mock"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OLLAMA = "ollama"


class Settings(BaseSettings):
    """Runtime configuration for the orchestration engine."""

    model_config = SettingsConfigDict(
        env_prefix="ORCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- core ---
    env: Environment = Environment.LOCAL
    service_name: str = "agent-orchestration-engine"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"

    # --- postgres (required; no sqlite fallback by design) ---
    pg_dsn: str = (
        "postgresql+asyncpg://orchestrator:orch_local_dev_only@127.0.0.1:5432/orchestration"
    )
    pg_test_dsn: str = (
        "postgresql+asyncpg://orchestrator:orch_local_dev_only@127.0.0.1:5432/orchestration_test"
    )
    pg_pool_size: int = Field(default=10, ge=1, le=100)
    pg_max_overflow: int = Field(default=5, ge=0, le=100)
    pg_statement_timeout_ms: int = Field(default=30_000, ge=1_000)
    pg_echo: bool = False

    # --- redis (required: locks, event streams, semaphores) ---
    redis_url: str = "redis://127.0.0.1:6379/1"
    redis_test_url: str = "redis://127.0.0.1:6379/15"
    redis_namespace: str = "orch"

    # --- pgvector ---
    embedding_dim: int = Field(default=768, ge=8, le=4096)
    vector_index: Literal["hnsw", "ivfflat", "none"] = "hnsw"

    # --- api ---
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_keys: str = "local_dev_key_change_me"
    api_require_auth: bool = True

    # --- concurrency ---
    max_concurrent_executions: int = Field(default=8, ge=1)
    max_concurrent_agents: int = Field(default=16, ge=1)
    max_concurrent_tools: int = Field(default=32, ge=1)

    # --- default budget ---
    budget_max_cost_usd: float = Field(default=0.50, gt=0)
    budget_max_tokens: int = Field(default=50_000, gt=0)
    budget_max_duration_seconds: float = Field(default=300.0, gt=0)
    budget_max_agent_steps: int = Field(default=30, gt=0)
    budget_max_tool_calls: int = Field(default=60, gt=0)
    budget_max_retries: int = Field(default=10, ge=0)

    # --- llm providers ---
    default_provider: ProviderName = ProviderName.MOCK
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)

    # --- observability ---
    tracing_enabled: bool = True
    trace_exporter: Literal["none", "console", "otlp"] = "none"
    otlp_endpoint: str = "http://127.0.0.1:4318/v1/traces"
    metrics_enabled: bool = True

    # --- safety switches ---
    enable_shell_tool: bool = False
    enable_python_tool: bool = True
    python_tool_timeout_seconds: float = Field(default=10.0, gt=0)
    approval_timeout_seconds: float = Field(default=3600.0, gt=0)
    file_sandbox_root: Path = Path("./.artifacts")

    @field_validator("pg_dsn", "pg_test_dsn")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        """Guard against a sync DSN silently blocking the event loop."""
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "PostgreSQL DSN must use the asyncpg driver "
                f"(postgresql+asyncpg://...), got: {v.split('://', 1)[0]}://"
            )
        return v

    @field_validator("redis_url", "redis_test_url")
    @classmethod
    def _reject_redis_db_zero(cls, v: str) -> str:
        """Refuse db 0 so a shared local Redis is never clobbered by the engine."""
        if v.rstrip("/").endswith(":6379") or v.rstrip("/").endswith("/0"):
            raise ValueError(
                "Refusing Redis db 0: pick an explicit non-zero db "
                "(e.g. redis://127.0.0.1:6379/1) so a shared local Redis is not clobbered."
            )
        return v

    @property
    def api_key_set(self) -> frozenset[str]:
        """Parsed set of accepted API keys."""
        return frozenset(k.strip() for k in self.api_keys.split(",") if k.strip())

    @property
    def pg_dsn_safe(self) -> str:
        """DSN with the password removed -- the only form safe to log or trace."""
        return _redact_dsn(self.pg_dsn)

    @property
    def redis_url_safe(self) -> str:
        """Redis URL with any password removed."""
        return _redact_dsn(self.redis_url)

    @property
    def is_production(self) -> bool:
        return self.env is Environment.PRODUCTION

    def redis_key(self, *parts: str) -> str:
        """Build a namespaced Redis key."""
        return ":".join((self.redis_namespace, *parts))


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (tests that monkeypatch the environment)."""
    get_settings.cache_clear()


_DSN_CREDENTIALS = re.compile(r"://([^:/@]+):([^@]+)@")


def _redact_dsn(dsn: str) -> str:
    """Replace the password component of a URL-style DSN with ``***``."""
    return _DSN_CREDENTIALS.sub(r"://\1:***@", dsn)
