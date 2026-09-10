"""Regression test (audit fix, task #4): channel ENDPOINT MANAGEMENT routes
(/channels/endpoints...) must 401 anonymous callers in enforced mode, exactly
like every other build surface - only the PROVIDER WEBHOOK RECEIVERS
(/channels/whatsapp/..., /telegram/..., /discord/..., /telnyx/..., /sms/...,
/email/...) are allowed to stay public, since providers cannot log in and
verify themselves instead (HMAC/Ed25519/secret-token/verify_token).

Before the fix, channels.py registered ALL of its routes - management
included - on a single router that main.py mounted WITHOUT the ENFORCED
dependency (the registration line's own comment claimed "endpoint mgmt
ENFORCED", but nothing actually applied it). An anonymous caller in enforced
mode could list, create, update, and delete another tenant's channel
endpoints (Slack/WhatsApp/Telegram/Discord/SMS/email integration configs
and credentials) with no token at all. The fix splits channels.py into
`router` (management, now mounted with dependencies=ENFORCED) and
`receivers_router` (the public webhook receivers, unchanged).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.config import settings
from app.main import app

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


def test_channels_management_401s_anonymous_in_enforced_mode():
    tag = uuid.uuid4().hex[:8]
    original = settings.require_auth
    try:
        async def _go():
            async with _client() as client:
                # register + auth a real user so we can prove the SAME routes
                # work fine for an authenticated caller (this isn't a routes-
                # are-broken bug, it's specifically an anonymous-bypass bug)
                res = await client.post(
                    "/auth/register",
                    json={"email": f"chanaudit-{tag}@py8n.test", "password": "pw12345678", "name": "Chan Audit"},
                )
                assert res.status_code == 201, res.text
                token = res.json()["token"]
                headers = {"Authorization": f"Bearer {token}"}

                settings.require_auth = True
                try:
                    # --- management surface: anonymous must be 401 ---
                    res = await client.get("/channels/adapters")
                    assert res.status_code == 401, res.text
                    res = await client.get("/channels/endpoints")
                    assert res.status_code == 401, res.text
                    res = await client.post(
                        "/channels/endpoints",
                        json={"provider": "telegram_bot_api", "name": "x", "config": {}},
                    )
                    assert res.status_code == 401, res.text

                    # --- same management surface: authenticated caller is fine ---
                    res = await client.get("/channels/adapters", headers=headers)
                    assert res.status_code == 200, res.text
                    res = await client.get("/channels/endpoints", headers=headers)
                    assert res.status_code == 200, res.text

                    # --- public provider receivers must stay reachable with
                    # no token at all (a real provider can't send one) ---
                    res = await client.get(
                        "/channels/whatsapp/does-not-exist/webhook",
                        params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"},
                    )
                    assert res.status_code == 404, res.text  # unknown endpoint, NOT 401
                    res = await client.post("/channels/telegram/does-not-exist/webhook", json={})
                    assert res.status_code == 404, res.text  # unknown endpoint, NOT 401
                finally:
                    settings.require_auth = False
        asyncio.run(_go())
    finally:
        settings.require_auth = original
