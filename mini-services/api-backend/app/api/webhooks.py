"""Public webhook catcher (Phase 4).

POST/GET /api/v1/webhooks/{workflow_id} — validates the workflow has an active
webhook_trigger node, injects the request envelope into the execution context
and dispatches asynchronously (or awaits the last node when configured).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Workflow
from ..services.dispatcher import dispatch_execution
from ..services.events import get_event_bus
from ..services.executor import execute_workflow
from ..services.webhook_info import public_webhook_url

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _load_webhook_workflow(workflow_id: str, db: AsyncSession) -> Workflow:
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Unknown webhook (workflow not found)")
    if not wf.is_active:
        raise HTTPException(status_code=409, detail="Workflow is inactive — activate it to enable its webhook")
    if not wf.webhook_nodes():
        raise HTTPException(status_code=409, detail="Workflow has no Webhook Trigger node")
    return wf


def _request_envelope(request: Request, body: object) -> dict:
    return {
        "method": request.method,
        "headers": dict(list(request.headers.items())[:40]),
        "query": dict(request.query_params),
        "body": body,
    }


@router.api_route("/{workflow_id}", methods=["POST", "GET", "PUT", "PATCH", "DELETE"], tags=["webhooks"])
async def catch_webhook(workflow_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    wf = await _load_webhook_workflow(workflow_id, db)
    body: object = None
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None

    node = wf.webhook_nodes()[0]
    params = node.get("parameters") or {}
    response_mode = params.get("response_mode", "immediately")
    envelope = _request_envelope(request, body)

    if response_mode == "last_node":
        # Run synchronously (bounded) and return the last node's output.
        try:
            result = await asyncio.wait_for(
                execute_workflow(
                    workflow_id,
                    trigger_type="webhook",
                    trigger_payload=envelope,
                    trigger_node_id=node["id"],
                ),
                timeout=settings.webhook_wait_seconds,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail=f"Execution exceeded {settings.webhook_wait_seconds}s wait limit")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        status_code = 200 if result["status"] == "success" else 500
        last_output = None
        if result["node_runs"]:
            last_output = result["node_runs"][-1].get("output")
        return JSONResponse(content={"status": result["status"], "execution_id": result["execution_id"], "last_output": last_output}, status_code=status_code)

    execution_id = await dispatch_execution(
        workflow_id,
        trigger_type="webhook",
        trigger_payload=envelope,
        trigger_node_id=node["id"],
    )
    return JSONResponse(
        content={
            "received": True,
            "execution_id": execution_id,
            "message": "Workflow triggered via webhook",
            "url": public_webhook_url(request, workflow_id),
        },
        status_code=202,
    )
