"""Helpers for rendering the public webhook URL."""

from __future__ import annotations

from fastapi import Request

from ..config import settings


def public_webhook_url(request: Request, workflow_id: str) -> str:
    if settings.public_base_url:
        base = settings.public_base_url.rstrip("/")
    else:
        base = str(request.base_url).rstrip("/")
    return f"{base}/api/v1/webhooks/{workflow_id}"
