"""WebSocket router (Phase 5) - live step-by-step execution progress.

GET /ws/executions/{execution_id}

Protocol (JSON frames, server -> client):
    {"event": "node_started"|"node_finished"|..., "node_id": ..., ...}
    {"event": "history", "events": [...]}    # replay of what already happened
    {"event": "execution_finished", ...}     # terminal frame, socket closes

Falls back gracefully: if the execution already finished, the client gets a
single history frame + terminal frame.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import decode_token
from ..config import settings
from ..db import AsyncSessionLocal
from ..models import ExecutionLog, User, Workflow
from ..services.events import get_event_bus

router = APIRouter()


@router.websocket("/ws/executions/{execution_id}")
async def execution_progress(websocket: WebSocket, execution_id: str):
    # --- auth (audit hardening) -------------------------------------------
    # Browsers cannot send headers on the WS handshake, so the token rides
    # the ``?token=`` query parameter. Enforced mode requires a valid JWT for
    # an existing user; legacy mode (require_auth=false) keeps accepting
    # anonymous connections (a still-valid token simply scopes what it sees).
    token = websocket.query_params.get("token") or ""
    user_id = decode_token(token) if token else None
    if settings.require_auth:
        if user_id is None:
            await websocket.close(code=4401)  # unauthenticated
            return
        async with AsyncSessionLocal() as session:
            known_user = await session.get(User, user_id) is not None
        if not known_user:
            await websocket.close(code=4401)
            return

    bus = get_event_bus()

    # Replay events already recorded in the DB (worker may have outrun us).
    async with AsyncSessionLocal() as session:
        log = await session.get(ExecutionLog, execution_id)
        if user_id is not None and log is not None:
            # Authed callers may only watch executions of visible workflows
            # (unclaimed or their own) - same rule as GET /executions/{id}.
            wf = await session.get(Workflow, log.workflow_id)
            if wf is not None and wf.owner_id is not None and wf.owner_id != user_id:
                await websocket.close(code=4404)  # looks nonexistent
                return
    await websocket.accept()
    terminal_seen = False
    if log is not None and log.node_runs:
        history = []
        history.append({"event": "execution_started", "execution_id": execution_id, "status": "running"})
        for run in log.node_runs:
            history.append({"event": "node_started", "execution_id": execution_id, "node_id": run["node_id"], "node_name": run.get("node_name"), "node_type": run.get("node_type"), "status": "running"})
            history.append({"event": "node_finished", "execution_id": execution_id, "node_id": run["node_id"], "node_name": run.get("node_name"), "node_type": run.get("node_type"), "status": run["status"], "duration_ms": run.get("duration_ms"), "output": run.get("output"), "error": run.get("error")})
        await websocket.send_text(json.dumps({"event": "history", "events": history}))
        if log.status != "running":
            terminal_seen = True
            await websocket.send_text(json.dumps({
                "event": "execution_finished",
                "execution_id": execution_id,
                "status": log.status,
                "error": log.error,
                "duration_ms": log.duration_ms,
                "node_runs": log.node_runs,
            }))

    if not terminal_seen:
        try:
            async def _pump() -> None:
                async for event in bus.subscribe(execution_id):
                    await websocket.send_text(json.dumps(event, default=str))
                    if event.get("event") == "execution_finished":
                        return

            await asyncio.wait_for(_pump(), timeout=300)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except asyncio.TimeoutError:
            await websocket.send_text(json.dumps({"event": "timeout", "execution_id": execution_id}))
        except Exception:  # noqa: BLE001
            pass

    try:
        await websocket.close()
    except Exception:  # noqa: BLE001
        pass
