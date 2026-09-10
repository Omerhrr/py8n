#!/usr/bin/env bash
# Py8n backend launcher (used by dev.sh mini-services mechanism and manual runs)
set -e
cd "$(dirname "$0")"

PY="python3"
if [ -x /home/z/.venv/bin/python3 ]; then
  PY="/home/z/.venv/bin/python3"
fi

mkdir -p data

# Self-heal: if core deps are missing (sandbox venv resets), reinstall quietly.
if ! "$PY" -c "import fastapi, sqlalchemy, aiosqlite, apscheduler, cryptography, httpx, jinja2" >/dev/null 2>&1; then
  echo "[py8n-backend] missing deps detected - installing requirements..."
  "$PY" -m pip install -q -r requirements.txt || echo "[py8n-backend] WARNING: pip install failed, continuing"
fi

# Apply/stamp Alembic migrations before the app touches the database (see
# migrations/bootstrap.py for the adoption story - existing installs get
# stamped at their current schema, fresh ones get the real migration chain).
# Never fatal: a migration failure here should surface loudly in the log
# rather than silently block boot in the sandbox default.
"$PY" -m migrations.bootstrap || echo "[py8n-backend] WARNING: migration bootstrap failed, continuing"

exec "$PY" -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PY8N_API_PORT:-8000}" \
  --log-level info
