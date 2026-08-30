"""Py8n application configuration (12-factor, env-driven)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Central settings object for the Py8n backend."""

    model_config = SettingsConfigDict(env_prefix="PY8N_", env_file=".env", extra="ignore")

    app_name: str = "Py8n"
    version: str = "1.22.0"
    debug: bool = True

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    # Sandbox default is SQLite; production (docker-compose) uses PostgreSQL.
    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'py8n.db'}"

    # ------------------------------------------------------------------
    # Execution mode: "inline" (single-process, sandbox default) or
    # "celery" (distributed workers backed by Redis in production).
    # ------------------------------------------------------------------
    execution_mode: str = "inline"

    # Celery / Redis broker. Only used when execution_mode == "celery".
    redis_url: str = "redis://127.0.0.1:6379/0"

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------
    # Fernet key used to encrypt credential payloads at rest. If empty a key
    # is generated once and persisted next to the database file.
    fernet_key: str = ""
    secret_key_file: Path = BASE_DIR / "data" / ".fernet.key"

    # Public base URL used to render shareable webhook URLs in the UI.
    # When empty the request's own base URL is used instead.
    public_base_url: str = ""

    # Sandbox LLM bridge (z-ai-web-dev-sdk Node service) used by the LLM node
    # when the user has not attached an OpenAI-compatible credential.
    llm_bridge_url: str = "http://127.0.0.1:3010"

    # Limits
    webhook_wait_seconds: int = 25          # max wait for response_mode=last_node
    max_output_capture: int = 20_000        # chars of node output persisted per node
    execution_history_limit: int = 200      # executions retained per workflow

    # CORS (self-hosted, permissive by default)
    cors_origins: list[str] = ["*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
