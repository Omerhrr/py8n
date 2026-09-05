"""V72 smoke helper: boot the real py8n app with
- a deterministic ASR engine registered under py8n_local (the registry is
  per-process by design; real deployments bind vosk/whisper.cpp),
- the REAL piper TTS bridge bound by the lifespan's bind_local_engines()
  through PY8N_PIPER_BIN / PY8N_PIPER_VOICE (the smoke points them at a
  stand-in piper that writes a valid wav - the probe, factory, registry
  and synthesize endpoint are all REAL).

Usage (the v72 smoke launches this, not you):
    python scripts/smoke_v72_server.py   # PORT env, default 8199
"""
from __future__ import annotations

import os
import sys

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from app.main import app  # noqa: E402
from app.services import voice_transport  # noqa: E402


def _deterministic_asr(pcm: bytes, sample_rate: int) -> dict:
    return {"transcript": "I want to order a laptop", "confidence": 0.93,
            "language": "en", "is_final": True}


voice_transport.register_asr_engine("py8n_local", _deterministic_asr)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8199")), log_level="warning")
