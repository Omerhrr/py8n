"""In-process rate limiting (audit hardening).

A tiny sliding-window limiter keyed by ``(bucket, client key)``. Every hit is
timestamped; a request is admitted while fewer than the bucket's limit of
hits fall inside the bucket's window. Buckets:

  auth      - POST /auth/register, POST /auth/login (brute-force throttle)
  webhook   - /webhooks/{id} ingest (flood throttle)
  chat      - /chat/{id} + /chat/{id}/stream (flood throttle)

Limits default to the config knobs (rate_limit_*_per_min, 60-second
windows). Tests can shrink buckets via the module-level ``OVERRIDES`` map
(bucket -> (limit, window_seconds)) and wipe counters with ``reset_all()`` -
both are plain module attributes/state so ``pytest`` monkeypatching works.

Kill switches (either one disables the limiter completely, checked per
request so flipping them needs no restart):
  * ``settings.rate_limit_enabled = false`` (PY8N_RATE_LIMIT_ENABLED=false)
  * environment ``PY8N_RATE_LIMIT_ENABLED=false`` (kept separate from
    config.py so deployments/tests can hard-disable without touching the
    settings object)

The limiter is per-process: behind multiple workers each worker keeps its
own counters (an acceptable trade for a self-hosted single-node app; a
shared-store limiter is the upgrade path if ever needed).
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Iterable

from fastapi import HTTPException, Request

from ..config import settings

# Bucket name -> default window in seconds. Windows are generous on purpose:
# the per-minute config numbers describe sustained load, and a sliding
# 60-second window already smooths bursts.
WINDOW_SECONDS = 60

# Tests may override per-bucket (limit, window) here, e.g.
#   monkeypatch.setattr(_ratelimit, "OVERRIDES", {"auth": (3, 60)})
OVERRIDES: dict[str, tuple[int, int]] = {}

# (bucket, client_key) -> timestamps of admitted hits inside the window.
_hits: dict[tuple[str, str], Deque[float]] = defaultdict(deque)

_FALSEY = {"", "0", "false", "no", "off"}


def _env_enabled() -> bool:
    # Unset -> enabled. Only an explicit falsey value turns the limiter off.
    return os.environ.get("PY8N_RATE_LIMIT_ENABLED", "true").strip().lower() not in _FALSEY


def _default_limit(bucket: str) -> int:
    return {
        "auth": settings.rate_limit_auth_per_min,
        "webhook": settings.rate_limit_webhook_per_min,
        "chat": settings.rate_limit_chat_per_min,
    }.get(bucket, 60)


def _bucket_rules(bucket: str) -> tuple[int, int]:
    """(limit, window_seconds) currently in force for a bucket."""
    override = OVERRIDES.get(bucket)
    if override is not None:
        return int(override[0]), int(override[1])
    return _default_limit(bucket), WINDOW_SECONDS


def client_key(request: Request) -> str:
    """Best-effort client identity: first X-Forwarded-For hop, else peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def check(bucket: str, request: Request) -> None:
    """Count one hit and raise 429 (+ Retry-After) when over the limit."""
    if not settings.rate_limit_enabled or not _env_enabled():
        return
    limit, window = _bucket_rules(bucket)
    if limit <= 0:  # window/limit disabled for this bucket
        return
    now = time.monotonic()
    hits = _hits[(bucket, client_key(request))]
    while hits and now - hits[0] >= window:
        hits.popleft()
    if len(hits) >= limit:
        retry_after = max(1, int(window - (now - hits[0])) + 1)
        raise HTTPException(
            status_code=429,
            detail="Too many requests - slow down",
            headers={"Retry-After": str(retry_after)},
        )
    hits.append(now)


def reset_all() -> None:
    """Drop every counter (tests call this between scenarios)."""
    _hits.clear()


def peek(bucket: str, request: Request) -> int:
    """Hits currently counted for (bucket, request's client) - tests/debug."""
    window = _bucket_rules(bucket)[1]
    now = time.monotonic()
    hits = _hits.get((bucket, client_key(request)))
    if not hits:
        return 0
    return sum(1 for t in hits if now - t < window)


def rate_limit(bucket: str) -> Callable[..., None]:
    """FastAPI dependency factory: ``Depends(rate_limit("auth"))``."""

    async def _dependency(request: Request) -> None:
        check(bucket, request)

    return _dependency


def known_buckets() -> Iterable[str]:
    return ("auth", "webhook", "chat")
