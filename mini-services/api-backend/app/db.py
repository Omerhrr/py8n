"""Async SQLAlchemy engine / session management."""

from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    """Declarative base for all Py8n ORM models."""


engine = create_async_engine(settings.database_url, echo=False, future=True)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create tables on startup (dev convenience; Alembic in production)."""
    from . import models  # noqa: F401  (register models)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Lightweight column migration for pre-existing dev databases
        # (create_all only adds missing TABLES, not missing COLUMNS).
        def _add_missing_columns(sync_conn) -> None:
            from sqlalchemy import inspect, text

            insp = inspect(sync_conn)
            cols = {c["name"] for c in insp.get_columns("workflows")}
            if "error_workflow_id" not in cols:
                sync_conn.execute(text("ALTER TABLE workflows ADD COLUMN error_workflow_id VARCHAR(36)"))
            if "tags" not in cols:
                sync_conn.execute(text("ALTER TABLE workflows ADD COLUMN tags JSON DEFAULT ('[]')"))
            if "folder_id" not in cols:  # v16
                sync_conn.execute(text("ALTER TABLE workflows ADD COLUMN folder_id VARCHAR(36)"))
            if "retention_days" not in cols:  # v20
                sync_conn.execute(text("ALTER TABLE workflows ADD COLUMN retention_days INTEGER"))
            # v37: ownership on every user-facing resource (NULL = unclaimed)
            for table in ("workflows", "datasets", "folders", "credentials", "env_variables", "apps", "dashboards"):
                cols = {c["name"] for c in insp.get_columns(table)}
                if "owner_id" not in cols:
                    sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN owner_id VARCHAR(36)"))
            # v43: vault hardening + key scopes + pack registries
            cred_cols = {c["name"] for c in insp.get_columns("credentials")}
            if "rotated_at" not in cred_cols:
                sync_conn.execute(text("ALTER TABLE credentials ADD COLUMN rotated_at TIMESTAMP"))
            key_cols = {c["name"] for c in insp.get_columns("api_keys")}
            if "scopes" not in key_cols:
                # NULL scopes = legacy unrestricted key (pre-v43 rows keep access)
                sync_conn.execute(text("ALTER TABLE api_keys ADD COLUMN scopes JSON"))

        await conn.run_sync(_add_missing_columns)
