"""V74 smoke helper: boot the real py8n app for the live smoke.

The model installs performed by the smoke itself (plus the whisper-cli
binary the smoke built) bind the REAL bridges through the documented
boot-time probe - the smoke verifies the Whisper bridge with the real
binary and the real ggml model, no stand-ins.

Usage (the v74 smoke launches this, not you):
    python scripts/smoke_v74_server.py   # PORT env, default 8202
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

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8202")), log_level="warning")
