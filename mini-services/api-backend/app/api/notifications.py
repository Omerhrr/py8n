"""Notification rules API (v44) - webhook-on-event configuration.

Endpoints (all under /notifications, enforced like the rest of the build surface):
  GET    /notifications            list the caller's rules
  GET    /notifications/events     the static event catalog
  POST   /notifications            create a rule
  PUT    /notifications/{id}       update (name/events/url/headers/scope/enabled)
  DELETE /notifications/{id}       drop the rule
  POST   /notifications/{id}/test  deliver a sample payload SYNCHRONOUSLY and
                                   report the outcome (ok, status_code, error)
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import NotificationRule, Workflow
from ..services import notifications as notif_svc

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    events: list[str] = Field(min_length=1, max_length=5)
    webhook_url: str = Field(min_length=1, max_length=2000)
    headers: dict[str, str] | None = Field(default=None, description="Extra headers, e.g. Authorization")
    workflow_id: str | None = Field(default=None, description="Scope to one workflow; NULL = all")
    enabled: bool = True


class NotificationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    events: list[str] | None = Field(default=None, min_length=1, max_length=5)
    webhook_url: str | None = Field(default=None, min_length=1, max_length=2000)
    headers: dict[str, str] | None = None
    workflow_id: str | None = None
    enabled: bool | None = None


def _out(row: NotificationRule) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "events": row.events or [],
        "webhook_url": row.webhook_url,
        "headers": row.headers or {},
        "workflow_id": row.workflow_id,
        "workflow_name": None,
        "enabled": bool(row.enabled),
        "created_at": row.created_at,
        "last_fired_at": row.last_fired_at,
        "fire_count": row.fire_count or 0,
        "last_status": row.last_status,
        "last_error": row.last_error,
    }


async def _with_workflow_name(db: AsyncSession, rows: list[NotificationRule]) -> list[dict]:
    out = []
    for r in rows:
        item = _out(r)
        if r.workflow_id:
            wf = await db.get(Workflow, r.workflow_id)
            item["workflow_name"] = wf.name if wf else None
        out.append(item)
    return out


def _validate_events(events: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(events))
    unknown = [e for e in cleaned if e not in notif_svc.NOTIFICATION_EVENTS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown event(s): {', '.join(unknown)}. Allowed: {', '.join(notif_svc.NOTIFICATION_EVENTS)}",
        )
    return cleaned


def _validate_url(url: str) -> str:
    try:
        return notif_svc._clean_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
async def list_rules(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(select(NotificationRule).order_by(NotificationRule.created_at.desc()))
    ).scalars().all()
    return await _with_workflow_name(db, [r for r in rows if r.owner_id is None or (user and r.owner_id == user.id)])


@router.get("/events")
async def event_catalog():
    """The static list of events rules can subscribe to."""
    return {"events": list(notif_svc.NOTIFICATION_EVENTS)}


@router.post("", status_code=201)
async def create_rule(body: NotificationCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    events = _validate_events(body.events)
    url = _validate_url(body.webhook_url)
    if body.workflow_id:
        wf = await db.get(Workflow, body.workflow_id)
        if wf is None:
            raise HTTPException(status_code=404, detail="Scoped workflow not found")
        own_or_404(wf.owner_id, user)
    row = NotificationRule(
        name=body.name.strip()[:120],
        events=events,
        webhook_url=url,
        headers={k: str(v) for k, v in (body.headers or {}).items()} or None,
        workflow_id=body.workflow_id,
        enabled=body.enabled,
    )
    row.owner_id = user.id if user else None
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return (await _with_workflow_name(db, [row]))[0]


async def _get_rule(db: AsyncSession, rule_id: str, user):
    row = await db.get(NotificationRule, rule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Notification rule not found")
    own_or_404(row.owner_id, user)  # v37: foreign rules look nonexistent
    return row


@router.put("/{rule_id}")
async def update_rule(rule_id: str, body: NotificationUpdate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_rule(db, rule_id, user)
    if body.name is not None:
        row.name = body.name.strip()[:120]
    if body.events is not None:
        row.events = _validate_events(body.events)
    if body.webhook_url is not None:
        row.webhook_url = _validate_url(body.webhook_url)
    if body.headers is not None:
        row.headers = {k: str(v) for k, v in body.headers.items()} or None
    if body.workflow_id is not None:
        if body.workflow_id:
            wf = await db.get(Workflow, body.workflow_id)
            if wf is None:
                raise HTTPException(status_code=404, detail="Scoped workflow not found")
            own_or_404(wf.owner_id, user)
        row.workflow_id = body.workflow_id or None
    if body.enabled is not None:
        row.enabled = body.enabled
    await db.commit()
    await db.refresh(row)
    return (await _with_workflow_name(db, [row]))[0]


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(rule_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_rule(db, rule_id, user)
    await db.delete(row)
    await db.commit()
    return None


@router.post("/{rule_id}/test")
async def test_rule(rule_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Synchronous sample delivery - the response tells you exactly what the
    webhook said, so wiring mistakes surface in seconds instead of never."""
    row = await _get_rule(db, rule_id, user)
    result = await notif_svc.test_fire(row)
    row.last_fired_at = datetime.now()
    row.fire_count = (row.fire_count or 0) + 1
    row.last_status = result["last_status"]
    row.last_error = result["last_error"]
    await db.commit()
    return result
