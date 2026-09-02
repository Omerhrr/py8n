"""Pytest bootstrap for the Py8n backend.

Guarantees the suite runs on a fresh clone with no manual setup:

- ``data/`` directories exist (SQLite cannot create missing parents itself,
  and tests bypass the FastAPI lifespan that would normally create them).
- A dedicated, throwaway per-run database is used instead of the developer's
  ``data/py8n.db`` so the suite never mutates or depends on live dev data
  and never trips over a stale schema missing newer columns.
- The schema is created once per session before any test runs.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Make `app` importable regardless of where pytest is invoked from.
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# --- isolated environment (must happen before any `app.*` import) ----------
_data_dir = BACKEND_DIR / "data"
_data_dir.mkdir(parents=True, exist_ok=True)
(_data_dir / "datasets").mkdir(parents=True, exist_ok=True)
(_data_dir / "artifacts").mkdir(parents=True, exist_ok=True)

# Throwaway DB, wiped at session start so the schema is always current.
_PYTEST_DB = _data_dir / "pytest.sqlite3"
if _PYTEST_DB.exists():
    _PYTEST_DB.unlink()

os.environ["PY8N_DATABASE_URL"] = f"sqlite+aiosqlite:///{_PYTEST_DB}"
os.environ["PY8N_EXECUTION_MODE"] = "inline"

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    """Create the full schema once; tests bypass the FastAPI lifespan."""
    from app.db import init_db

    asyncio.run(init_db())
    yield
