"""V73 smoke helper: boot the real py8n app with NO stand-in engines -
the model installs performed by the smoke itself bind the REAL vosk ASR
bridge and the REAL piper TTS bridge through the documented boot-time
probe (data/models defaults), which is exactly what the smoke verifies.

Usage (the v73 smoke launches this, not you):
    python scripts/smoke_v73_server.py   # PORT env, default 8201
"""
from __future__ import annotations

import os
import sys

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from app.main import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8201")), log_level="warning")
