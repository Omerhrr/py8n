"""Public chat endpoint (v25) — conversational workflows.

POST /api/v1/chat/{workflow_id} — validates the workflow has an active
chat_trigger node, starts one run per message and replies to the chat client
either from the last node's output (response_mode=last_node) or from a
Respond to Webhook node mid-flow (response_mode=respond_node, reusing the v21
WebhookResponder channel).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Workflow
from ..services.executor import _background_tasks, execute_workflow
from .webhooks import WebhookResponder

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatMessage(BaseModel):
    message: str = Field(..., description="The user's chat message")
    session_id: str = Field(default="default", max_length=255, description="Conversation key — stable per chat window, used for session memory")


def _extract_reply(last_output: Any) -> str:
    """Best-effort plain-text reply from the last node's output.

    Strings pass through; dicts are probed for common answer keys first and
    JSON-dumped otherwise; anything else is str()'d.
    """
    if last_output is None:
        return ""
    if isinstance(last_output, str):
        return last_output
    if isinstance(last_output, dict):
        for key in ("answer", "reply", "text", "message", "output"):
            value = last_output.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(last_output, ensure_ascii=False, default=str)
    if isinstance(last_output, list):
        return _extract_reply(last_output[-1] if last_output else None)
    return str(last_output)


async def _load_chat_workflow(workflow_id: str, db: AsyncSession) -> Workflow:
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Unknown chat (workflow not found)")
    if not wf.is_active:
        raise HTTPException(status_code=409, detail="Workflow is inactive — activate it to enable its chat")
    if not wf.chat_nodes():
        raise HTTPException(status_code=409, detail="Workflow has no Chat Trigger node")
    return wf


@router.post("/{workflow_id}", tags=["chat"])
async def send_chat_message(workflow_id: str, msg: ChatMessage, db: AsyncSession = Depends(get_db)):
    wf = await _load_chat_workflow(workflow_id, db)
    node = wf.chat_nodes()[0]
    params = node.get("parameters") or {}
    response_mode = params.get("response_mode", "last_node")
    trigger_payload = {
        "session_id": msg.session_id,
        "message": msg.message,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }

    if response_mode == "respond_node":
        # v21 channel reused: wait for a Respond to Webhook node (or flow end).
        responder = WebhookResponder()
        flow_task = asyncio.create_task(
            execute_workflow(
                workflow_id,
                trigger_type="chat",
                trigger_payload=trigger_payload,
                trigger_node_id=node["id"],
                respond_channel=responder,
            )
        )
        _background_tasks.add(flow_task)
        flow_task.add_done_callback(_background_tasks.discard)
        waiter = asyncio.create_task(responder.event.wait())
        done, _pending = await asyncio.wait(
            {flow_task, waiter},
            timeout=settings.webhook_wait_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if waiter in done:
            if isinstance(responder.body, (dict, list)):
                return JSONResponse(content=responder.body, status_code=responder.status)
            return PlainTextResponse(
                str(responder.body), status_code=responder.status, media_type="text/plain"
            )
        waiter.cancel()
        if flow_task in done:
            if flow_task.exception() is not None:
                raise HTTPException(status_code=500, detail=f"Workflow failed before responding: {flow_task.exception()}")
            result = flow_task.result()
            if result.get("status") == "error":
                raise HTTPException(status_code=500, detail=f"Workflow errored before responding: {result.get('error')}")
            raise HTTPException(
                status_code=404,
                detail="Workflow finished without calling a Respond to Webhook node",
            )
        raise HTTPException(
            status_code=504,
            detail=f"Timed out after {settings.webhook_wait_seconds}s waiting for a Respond to Webhook node — the workflow keeps running in the background",
        )

    # last_node (default): run synchronously (bounded) and reply with the
    # final node output, extracted to a plain-text "reply" for chat UIs.
    try:
        result = await asyncio.wait_for(
            execute_workflow(
                workflow_id,
                trigger_type="chat",
                trigger_payload=trigger_payload,
                trigger_node_id=node["id"],
            ),
            timeout=settings.webhook_wait_seconds,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Execution exceeded {settings.webhook_wait_seconds}s wait limit") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    last_output = None
    if result["node_runs"]:
        last_output = result["node_runs"][-1].get("output")
    status_code = 200 if result["status"] == "success" else 500
    return JSONResponse(
        content={
            "status": result["status"],
            "execution_id": result["execution_id"],
            "session_id": msg.session_id,
            "reply": _extract_reply(last_output),
            "output": last_output,
        },
        status_code=status_code,
    )
