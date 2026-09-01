"""Notification rules (v44) - webhook-on-event dispatch.

A rule says: when one of my events happens (a run succeeded / failed / was
cancelled), POST a JSON payload to my webhook URL. Dispatch is
fire-and-forget: matching rules are collected synchronously, each delivery
runs in its own task with a 10s timeout, and a dead webhook can never slow
or break a run. Delivery stats (last_fired_at, fire_count, last_status,
last_error) land on the rule row for at-a-glance health.

Rules scope two ways:
  - events: subset of NOTIFICATION_EVENTS
  - workflow_id: NULL = every workflow the owner could see; a specific id
    restricts the rule to that workflow
Cross-user noise guard: a rule owned by user A never fires for events on
workflows owned by user B (in open mode, unclaimed/NULL ownership fires all).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import AsyncSessionLocal
from ..models import NotificationRule

logger = logging.getLogger("py8n.notifications")

NOTIFICATION_EVENTS = ("execution_succeeded", "execution_failed", "execution_cancelled")

FIRE_TIMEOUT_SECONDS = 10.0

# Delivery tasks spawned by dispatch(); tests/smoke drain via drain_pending()
_pending: set[asyncio.Task] = set()


def _clean_url(url: str) -> str:
    url = (url or "").strip()
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme not in ("http", "https"):
        raise ValueError("Webhook URL must start with http:// or https://")
    return url


async def _deliver(rule_id: str, url: str, headers: dict | None, payload: dict) -> None:
    """One delivery attempt; stats land on the rule row. Never raises."""
    status = "error"
    error: str | None = None
    status_code: int | None = None
    try:
        async with httpx.AsyncClient(timeout=FIRE_TIMEOUT_SECONDS, follow_redirects=True) as client:
            resp = await client.post(url, json=payload, headers=headers or None)
        status_code = resp.status_code
        if resp.status_code >= 400:
            error = f"webhook returned HTTP {resp.status_code}"
        else:
            status = "ok"
    except (httpx.HTTPError, ValueError) as exc:
        error = f"{exc.__class__.__name__}: {exc}"[:290]

    try:
        async with AsyncSessionLocal() as session:
            row = await session.get(NotificationRule, rule_id)
            if row is not None:
                row.last_fired_at = datetime.now(timezone.utc)
                row.fire_count = (row.fire_count or 0) + 1
                row.last_status = status
                row.last_error = error
                await session.commit()
    except Exception:  # noqa: BLE001 - stats must never break the caller
        logger.exception("notification stats write failed for rule %s", rule_id)
    if error:
        logger.warning("notification rule %s delivery failed: %s", rule_id, error)


def drain_pending() -> None:
    """Best-effort sync no-op kept for API symmetry with the executor drain."""
    return None


async def adrain_pending() -> None:
    """Await every in-flight delivery (tests use this for determinism)."""
    if _pending:
        await asyncio.gather(*[t for t in list(_pending) if not t.done()], return_exceptions=True)
        _pending.clear()


async def dispatch(
    event: str,
    payload: dict,
    workflow_id: str | None,
    workflow_owner_id: str | None = None,
    db: AsyncSession | None = None,
) -> int:
    """Fire every enabled rule matching this event. Returns the number of
    deliveries spawned; the deliveries themselves run in the background."""
    if event not in NOTIFICATION_EVENTS:
        return 0

    async def _match() -> list[NotificationRule]:
        if db is not None:
            rows = (
                await db.execute(select(NotificationRule).where(NotificationRule.enabled.is_(True)))
            ).scalars().all()
        else:
            async with AsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        select(NotificationRule).where(NotificationRule.enabled.is_(True))
                    )
                ).scalars().all()
        return [r for r in rows if _matches(r, event, workflow_id, workflow_owner_id)]

    rules = await _match()
    stamp = {**payload, "event": event, "ts": datetime.now(timezone.utc).isoformat()}
    for rule in rules:
        task = asyncio.create_task(
            _deliver(rule.id, rule.webhook_url, rule.headers, stamp)
        )
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    return len(rules)


def _matches(rule: NotificationRule, event: str, workflow_id: str | None, workflow_owner_id: str | None) -> bool:
    if event not in (rule.events or []):
        return False
    if rule.workflow_id and rule.workflow_id != workflow_id:
        return False
    # cross-user guard: alice's catch-all rule does not fire on bob's runs
    if rule.owner_id and workflow_owner_id and rule.owner_id != workflow_owner_id:
        return False
    return True


async def test_fire(rule: NotificationRule) -> dict:
    """Deliver a sample payload SYNCHRONOUSLY so the caller sees the result."""
    payload = {
        "event": "test",
        "ts": datetime.now(timezone.utc).isoformat(),
        "message": f"Py8n test fire for rule {rule.name!r}",
        "execution_id": None,
        "workflow_id": rule.workflow_id,
        "workflow_name": None,
        "status": "test",
    }
    try:
        async with httpx.AsyncClient(timeout=FIRE_TIMEOUT_SECONDS, follow_redirects=True) as client:
            resp = await client.post(rule.webhook_url, json=payload, headers=rule.headers or None)
        ok = resp.status_code < 400
        row_update = {
            "last_status": "ok" if ok else "error",
            "last_error": None if ok else f"webhook returned HTTP {resp.status_code}",
        }
        return {"ok": ok, "status_code": resp.status_code, **row_update}
    except (httpx.HTTPError, ValueError) as exc:
        msg = f"{exc.__class__.__name__}: {exc}"[:290]
        return {"ok": False, "status_code": None, "last_status": "error", "last_error": msg}
