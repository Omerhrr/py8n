"""Alembic environment - wired to the app's own Settings + ORM metadata.

Unlike a stock Alembic env.py, this one never reads ``sqlalchemy.url`` from
alembic.ini (it's left blank there on purpose) - the URL always comes from
``app.config.settings.database_url``, the exact same source db.py uses. That
means `alembic upgrade head` against a fresh checkout, a docker-compose
Postgres cluster or a custom PY8N_DATABASE_URL all resolve to the database
the app itself would connect to - one source of truth, never two configs to
keep in sync.
"""

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# app/ lives one directory up from migrations/ (mini-services/api-backend/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import Base  # noqa: E402
from app import models  # noqa: E402,F401  (registers every model on Base.metadata)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Drive both offline and online modes off the app's own database URL rather
# than a value duplicated into alembic.ini (which would silently drift from
# PY8N_DATABASE_URL / docker-compose's Postgres URL otherwise).
config.set_main_option("sqlalchemy.url", settings.database_url)

# add your model's MetaData object here for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
