"""Apply Alembic migrations at process start - the boot-time entry point.

Run as ``python -m migrations.bootstrap`` from mini-services/api-backend/,
BEFORE the server starts (see start.sh / Dockerfile). Deliberately a plain
synchronous script, not an import inside the FastAPI app: Alembic's async
env.py drives itself with ``asyncio.run(...)``, which cannot nest inside an
event loop the app is already running (FastAPI's own lifespan). Running as
a separate process before uvicorn starts sidesteps that entirely - the
same pattern as ``python manage.py migrate`` in other frameworks.

Adoption story (why this isn't just ``alembic upgrade head``)
---------------------------------------------------------------
This script runs BEFORE db.py's own ``init_db()`` (create_all() + the
legacy ``_add_missing_columns`` shim), which still runs unchanged, every
boot, as a safety net - so getting the ordering below right matters:

* Table ``workflows`` does not exist yet -> a genuinely fresh database.
  Run the real migration chain (``alembic upgrade head``): the baseline
  revision's own DDL creates every table, so Alembic is what lays down
  the schema. db.py's create_all() runs moments later and is then a pure
  no-op (every table already exists).
* ``workflows`` exists but there is no ``alembic_version`` table -> an
  install that predates this migration - schema laid down over time by
  create_all() + the hand-written shim. Verified (autogenerate reports
  zero diff) that this is byte-for-byte the same shape the baseline
  migration describes, so STAMP it at the baseline revision instead of
  re-running its DDL against tables that already exist. No rows change.
* ``alembic_version`` already present -> a normal ``alembic upgrade
  head``: applies anything added after the baseline, no-ops if current.

Net effect: nothing about today's boot path is removed (create_all() and
the shim keep running, unchanged, as a safety net), and every schema
change from here on is a real, reviewable Alembic migration instead of a
new branch in that shim.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, inspect as sa_inspect  # noqa: E402

from app.config import settings  # noqa: E402


def _sync_url(database_url: str) -> str:
    """The app's async URL, downgraded to a sync driver for this one-shot check.

    sqlite+aiosqlite -> sqlite (stdlib sqlite3); postgresql+asyncpg ->
    postgresql+psycopg2 (already an optional dependency - requirements.txt
    pins psycopg2-binary for the v50 postgres connector node). Any other
    scheme is passed through unchanged and left to whatever sync driver is
    installed.
    """
    if database_url.startswith("sqlite+aiosqlite"):
        return database_url.replace("sqlite+aiosqlite", "sqlite", 1)
    if database_url.startswith("postgresql+asyncpg"):
        return database_url.replace("postgresql+asyncpg", "postgresql+psycopg2", 1)
    return database_url


def main() -> None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))

    sync_url = _sync_url(settings.database_url)
    # sqlite: make sure the data/ directory exists before sqlite3 tries to
    # open/create the file (db.py normally does this via create_all(), which
    # has not run yet on a fresh install - this script runs first).
    if sync_url.startswith("sqlite"):
        db_path = sync_url.split("///", 1)[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(sync_url)
    try:
        insp = sa_inspect(engine)
        has_workflows = insp.has_table("workflows")
        has_alembic_table = insp.has_table("alembic_version")
    finally:
        engine.dispose()

    if not has_workflows:
        print("[migrations] fresh database - running the full migration chain.")
        command.upgrade(cfg, "head")
    elif not has_alembic_table:
        print("[migrations] pre-Alembic install detected (schema already matches "
              "the baseline via create_all() + the legacy column shim) - "
              "stamping instead of re-running DDL.")
        command.stamp(cfg, "head")
    else:
        print("[migrations] alembic_version present - applying anything newer.")
        command.upgrade(cfg, "head")
    print("[migrations] up to date.")


if __name__ == "__main__":
    main()
