"""Events API (v80) - the real-time event system over HTTP + WS.

* ``GET  /events``          - the owner's events, newest first, filtered
                              (type prefix or pattern, source, session,
                              correlation, since/until)
* ``GET  /events/{id}``     - one event
* ``POST /events``          - the manual door: a workflow (http node), an
                              operator, or a test emits an event of its own
                              and every Event Trigger pattern matching it
                              fires - composition in both directions
* ``WS   /events/stream``   - the live tail (token rides ``?token=``, the
                              same handshake the execution sockets use)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import decode_token, get_optional_user
from ..config import settings
from ..db import AsyncSessionLocal, get_db
from ..models import User
from ..services import system_events as events_svc
from ..services.system_events import SystemEventError

router = APIRouter(prefix="/events", tags=["events"])

# the live tail rides its OWN router: websockets cannot send the enforced
# auth headers, so the stream authenticates via the token query param
# (the ws.py pattern) and is included without the enforced dependency
stream_router = APIRouter(prefix="/events", tags=["events"])


def _http(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


class EventEmit(BaseModel):
    type: str = Field(..., min_length=1, max_length=80,
                      description="dotted event type (crm.lead_won, queue.position_changed)")
    source: str = Field(..., min_length=1, max_length=40,
                        description="the emitting component (voice|queue|sms|meeting|video|recording|media|campaign|user|system)")
    actor: str = Field(default="", max_length=180)
    target_type: str = Field(default="", max_length=40)
    target_id: str = Field(default="", max_length=180)
    payload: dict = Field(default_factory=dict)
    correlation_id: str = Field(default="", max_length=180)
    session_id: str | None = None


@router.post("", status_code=201)
async def emit_event(body: EventEmit, user=Depends(get_optional_user),
                     db: AsyncSession = Depends(get_db)):
    try:
        out = await events_svc.emit(db, getattr(user, "id", None), body.type,
                                    source=body.source, actor=body.actor,
                                    target_type=body.target_type,
                                    target_id=body.target_id,
                                    payload=body.payload,
                                    correlation_id=body.correlation_id,
                                    session_id=body.session_id)
        await db.commit()
        return out
    except SystemEventError as exc:
        raise _http(exc) from exc


@router.get("")
async def list_events(
    type: str | None = Query(default=None, alias="type",
                             description="dotted prefix (queue.) or pattern (queue.*)"),
    source: str | None = None,
    session_id: str | None = None,
    correlation_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    try:
        return {"events": await events_svc.list_events(
            db, getattr(user, "id", None), type_prefix=type, source=source,
            session_id=session_id, correlation_id=correlation_id,
            since=since, until=until, limit=limit)}
    except SystemEventError as exc:
        raise _http(exc) from exc


@router.get("/contracts")
async def event_contracts():
    """The event system's own contract: sources, the catalog of types the
    primitives emit, the trigger pattern rules - derived, nothing stored."""
    return {
        "sources": list(events_svc.EVENT_SOURCES),
        "types_emitted": {
            "voice": ["call.created", "call.started", "call.ended"],
            "queue": ["call.waiting", "call.seated", "queue.left",
                      "queue.position_changed"],
            "sms": ["sms.sent", "sms.received"],
            "meeting": ["participant.joined", "meeting.ended", "chat.posted",
                        "hand.raised", "floor.granted", "floor.released"],
            "video": ["track.published", "track.unpublished"],
            "recording": ["recording.started", "recording.ready"],
            "media": ["media.session_started", "media.session_ended",
                      "media.participant_added"],
            "campaign": ["callback.scheduled"],
            # v81: the system runtime's own operations ride the same door -
            # a workflow can subscribe to system.* and react to the platform
            "system": ["system.installed", "system.started", "system.stopped",
                       "system.paused", "system.resumed", "system.upgraded",
                       "system.component_added", "system.component_removed",
                       "system.built"],  # v82: the AI composer builds a system
            # v84: the business state machines move - long-running autonomy
            "business": ["business.state_changed"],
        },
        "trigger_node": {
            "type": "event_trigger",
            "params": {"event_type": "exact type or fnmatch pattern (queue.*)",
                       "source": "optional source filter"},
            "note": ("the workflow re-runs for EVERY matching event; "
                     "dispatch is fire-and-forget and lands in the run's own log"),
        },
        "shape": {"source": "the emitting component", "type": "dotted what-happened",
                  "actor": "who did it", "target": "entity type + id",
                  "payload": "the event's own data", "correlation_id": "the journey thread",
                  "session_id": "the live call when one exists"},
        "live": events_svc.stream_out_note(),
    }


@router.get("/{event_id}")
async def get_event(event_id: str, user=Depends(get_optional_user),
                    db: AsyncSession = Depends(get_db)):
    try:
        return await events_svc.get_event(db, getattr(user, "id", None), event_id)
    except SystemEventError as exc:
        raise _http(exc, 404) from exc


@stream_router.websocket("/stream")
async def event_stream(websocket: WebSocket):
    """The live tail. Owner-scoped: the token's user sees their own events
    (and unowned ones); unauthenticated tails exist only in open mode and
    see only unowned events."""
    token = websocket.query_params.get("token") or ""
    user_id = decode_token(token) if token else None
    if settings.require_auth:
        if user_id is None:
            await websocket.close(code=4401)
            return
        async with AsyncSessionLocal() as session:
            known = await session.get(User, user_id) is not None
        if not known:
            await websocket.close(code=4401)
            return
    owner_key = user_id  # None -> the anonymous "*" bucket
    queue = events_svc.subscribe(owner_key)
    await websocket.accept()
    try:
        await websocket.send_text(json.dumps({
            "event": "stream_started",
            "scope": "owner" if user_id else "*",
            **events_svc.stream_out_note()}))
        while True:
            event = await queue.get()
            await websocket.send_text(json.dumps({"event": "system_event",
                                                  **event}))
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - a dead socket just leaves quietly
        pass
    finally:
        events_svc.unsubscribe(owner_key, queue)
