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
    version: str = "1.52.0"
    # Audit hardening: dev-only convenience surfaces default OFF. Production
    # docker-compose already pins PY8N_DEBUG=false explicitly.
    debug: bool = False

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

    # v37: multi-user auth. False = single-user legacy mode (anonymous works,
    # tokens still scope). True = enforced mode (anonymous gets 401 on every
    # build/admin surface; webhooks, chat and published runtimes stay open).
    require_auth: bool = False
    jwt_secret_file: Path = BASE_DIR / "data" / ".jwt.key"
    token_ttl_seconds: int = 7 * 24 * 3600  # 7 days

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

    # v27: parquet files for first-class datasets live here
    datasets_dir: Path = BASE_DIR / "data" / "datasets"

    # v28: chart PNGs / model pickles produced by workflow runs live here
    artifacts_dir: Path = BASE_DIR / "data" / "artifacts"

    # CORS (self-hosted, permissive by default)
    cors_origins: list[str] = ["*"]

    # ------------------------------------------------------------------
    # Security hardening (audit)
    # ------------------------------------------------------------------
    # /_spawn sandbox-control endpoint: disabled unless explicitly enabled.
    # Token is per-boot random when empty (never a static shared secret).
    spawn_enabled: bool = False
    spawn_token: str = ""

    # In-process rate limiting (per client IP per route class, per minute).
    rate_limit_enabled: bool = True
    rate_limit_auth_per_min: int = 120
    rate_limit_webhook_per_min: int = 300
    rate_limit_chat_per_min: int = 120

    # Webhook ingest: cap request body size (413 above this).
    max_webhook_body_bytes: int = 2_000_000

    # Pack registry fetches: hard cap on downloaded bytes (64 MB).
    max_registry_fetch_bytes: int = 64_000_000

    # Dataset run_sql: hard cap on returned rows.
    max_sql_rows: int = 10_000

    # ------------------------------------------------------------------
    # v51: dataset storage backend - where dataset parquet blobs live.
    # "local" (default) = data/datasets/ on disk, exactly as before;
    # "s3" = AWS S3 or any S3-compatible store (set s3_endpoint_url for
    # MinIO/Wasabi/R2); "gcs" = Google Cloud Storage. DuckDB remains the
    # compute engine either way - backends only move parquet bytes.
    # ------------------------------------------------------------------
    storage_backend: str = "local"
    s3_bucket: str = ""
    s3_prefix: str = ""                      # optional key prefix inside the bucket
    s3_endpoint_url: str = ""                # e.g. http://minio:9000 for MinIO
    s3_region: str = ""
    s3_access_key_id: str = ""               # empty = boto3 default chain
    s3_secret_access_key: str = ""
    gcs_bucket: str = ""
    gcs_prefix: str = ""

    # ------------------------------------------------------------------
    # v52: scheduled report delivery (webhook + email channels).
    # Reports can push their generated artifact out when they fire:
    # webhook = HTTP POST of a JSON envelope (optionally with the file
    # base64-attached); email = SMTP with the report attached. SMTP is
    # disabled while smtp_host is empty - delivery events then record a
    # clear "skipped" instead of failing the report run.
    # ------------------------------------------------------------------
    smtp_host: str = ""                      # empty = email delivery disabled
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "py8n@localhost"
    smtp_use_tls: bool = True                # STARTTLS (port 587 typical)
    webhook_delivery_timeout_seconds: int = 10
    max_delivery_attachment_bytes: int = 8_000_000   # skip attaching bigger files


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
