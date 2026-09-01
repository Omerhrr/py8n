"""Public chat endpoint (v25) - conversational workflows.

POST /api/v1/chat/{workflow_id} - validates the workflow has an active
chat_trigger node, starts one run per message and replies to the chat client
either from the last node's output (response_mode=last_node) or from a
Respond to Webhook node mid-flow (response_mode=respond_node, reusing the v21
WebhookResponder channel).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Workflow
from ..services.events import get_event_bus
from ..services.executor import _background_tasks, execute_workflow
from .webhooks import WebhookResponder

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatMessage(BaseModel):
    message: str = Field(..., description="The user's chat message")
    session_id: str = Field(default="default", max_length=255, description="Conversation key - stable per chat window, used for session memory")


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
        raise HTTPException(status_code=409, detail="Workflow is inactive - activate it to enable its chat")
    if not wf.chat_nodes() and not _agent_nodes(wf):
        raise HTTPException(status_code=409, detail="Workflow has no Chat Trigger or AI Agent node")
    return wf


def _agent_nodes(wf: Workflow) -> list[dict]:
    """v34: agent-only workflows (no Chat Trigger) are chat-able too."""
    return [n for n in (wf.graph or {}).get("nodes", []) if n.get("type") == "ai_agent"]


def _chat_anchor(wf: Workflow, msg: ChatMessage) -> tuple[dict, dict]:
    """Pick the trigger node + payload for a chat run.

    chat_trigger workflows (v25): the chat node anchors and the message sits
    at the top level of the trigger payload.

    agent-only workflows (v34): anchor on the manual trigger (or the agent
    itself when there is no manual trigger) and ALSO nest the message under
    'payload' - that is the key ManualTriggerNode merges over its static
    payload, so one workflow can answer both editor Runs and console chats.
    """
    chat_nodes = wf.chat_nodes()
    received_at = datetime.now(timezone.utc).isoformat()
    if chat_nodes:
        node = chat_nodes[0]
        payload = {
            "session_id": msg.session_id,
            "message": msg.message,
            "received_at": received_at,
        }
        return node, payload
    agents = _agent_nodes(wf)
    manual = next((n for n in (wf.graph or {}).get("nodes", []) if n.get("type") == "manual_trigger"), None)
    node = manual or agents[0]
    payload = {
        "session_id": msg.session_id,
        "message": msg.message,
        "received_at": received_at,
        "payload": {"message": msg.message, "session_id": msg.session_id},
    }
    return node, payload


@router.post("/{workflow_id}", tags=["chat"])
async def send_chat_message(workflow_id: str, msg: ChatMessage, db: AsyncSession = Depends(get_db)):
    wf = await _load_chat_workflow(workflow_id, db)
    node, trigger_payload = _chat_anchor(wf, msg)
    params = node.get("parameters") or {}
    response_mode = params.get("response_mode", "last_node")

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
            detail=f"Timed out after {settings.webhook_wait_seconds}s waiting for a Respond to Webhook node - the workflow keeps running in the background",
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


# ---------------------------------------------------------------------------
# v26: SSE progress stream - POST /chat/{id}/stream
# Same validation and run semantics as the plain endpoint, but the client
# receives live frames while the flow runs:
#   event: start    data: {execution_id, session_id}
#   event: node     data: {node_id, node_name, node_type, status, duration_ms?, error?}
#   event: done     data: {status, execution_id, session_id, reply, output}
#   event: error    data: {error, execution_id}
#   event: timeout  data: {after_seconds}   # flow keeps running in background
# ---------------------------------------------------------------------------

def _sse_frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/{workflow_id}/stream", tags=["chat"])
async def stream_chat_message(workflow_id: str, msg: ChatMessage, db: AsyncSession = Depends(get_db)):
    # Validate BEFORE establishing the stream so clients get normal JSON errors.
    wf = await _load_chat_workflow(workflow_id, db)
    node, trigger_payload = _chat_anchor(wf, msg)
    params = node.get("parameters") or {}
    response_mode = params.get("response_mode", "last_node")

    bus = get_event_bus()
    execution_id = uuid.uuid4().hex
    responder: WebhookResponder | None = WebhookResponder() if response_mode == "respond_node" else None

    # The pump must be created BEFORE the flow task: tasks run in creation
    # order, so the bus subscription is registered before the first event is
    # published. (The bus.subscribe generator only registers its queue on the
    # first __anext__, which happens when the pump task first runs.)
    queue: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        async for event in bus.subscribe(execution_id):
            await queue.put(event)

    pump_task = asyncio.create_task(pump())
    flow_task = asyncio.create_task(
        execute_workflow(
            workflow_id,
            trigger_type="chat",
            trigger_payload=trigger_payload,
            trigger_node_id=node["id"],
            execution_id=execution_id,
            respond_channel=responder,
        )
    )
    _background_tasks.add(flow_task)
    flow_task.add_done_callback(_background_tasks.discard)

    async def event_stream():
        waiter: asyncio.Event | None = responder.event if responder is not None else None
        try:
            yield _sse_frame("start", {"execution_id": execution_id, "session_id": msg.session_id})
            deadline = asyncio.get_event_loop().time() + settings.webhook_wait_seconds
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    yield _sse_frame("timeout", {"after_seconds": settings.webhook_wait_seconds})
                    return
                # respond_node mode: the responder's answer races the bus events -
                # first one wins, exactly like the plain endpoint.
                if waiter is not None:
                    got_responder = asyncio.create_task(waiter.wait())
                    got_event = asyncio.create_task(queue.get())
                    done_set, _pending = await asyncio.wait(
                        {got_responder, got_event},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in _pending:
                        t.cancel()
                    if got_responder in done_set:
                        yield _sse_frame("done", {
                            "status": "success",
                            "execution_id": execution_id,
                            "session_id": msg.session_id,
                            "reply": _extract_reply(responder.body),
                            "output": responder.body,
                            "via": "respond_node",
                        })
                        return  # flow keeps running in the background
                    if got_event not in done_set:
                        yield _sse_frame("timeout", {"after_seconds": settings.webhook_wait_seconds})
                        return
                    event = got_event.result()
                else:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except asyncio.TimeoutError:
                        yield _sse_frame("timeout", {"after_seconds": settings.webhook_wait_seconds})
                        return
                kind = event.get("event")
                if kind == "node_started":
                    yield _sse_frame("node", {
                        "node_id": event.get("node_id"),
                        "node_name": event.get("node_name"),
                        "node_type": event.get("node_type"),
                        "status": "running",
                    })
                elif kind == "node_finished":
                    yield _sse_frame("node", {
                        "node_id": event.get("node_id"),
                        "node_name": event.get("node_name"),
                        "node_type": event.get("node_type"),
                        "status": event.get("status"),
                        "duration_ms": event.get("duration_ms"),
                        "error": event.get("error"),
                    })
                elif kind.startswith("agent_"):
                    # v36: live agent trace - the loop's iterations, raw model
                    # replies, tool calls and results stream out as they happen
                    phase = kind[len("agent_"):]
                    frame = {"phase": phase, "execution_id": execution_id}
                    for key in ("iteration", "max_iterations", "reply", "tool", "arguments", "status", "preview", "answer", "data"):
                        if key in event:
                            frame[key] = event[key]
                    yield _sse_frame("agent", frame)
                elif kind == "execution_finished":
                    result_status = event.get("status", "success")
                    if result_status == "error":
                        yield _sse_frame("error", {"error": event.get("error") or "workflow failed", "execution_id": execution_id})
                        return
                    if responder is not None and responder.responded:
                        yield _sse_frame("done", {
                            "status": result_status,
                            "execution_id": execution_id,
                            "session_id": msg.session_id,
                            "reply": _extract_reply(responder.body),
                            "output": responder.body,
                            "via": "respond_node",
                        })
                        return
                    node_runs = event.get("node_runs") or []
                    last_output = node_runs[-1].get("output") if node_runs else None
                    if responder is not None:
                        # respond_node flow finished WITHOUT answering
                        yield _sse_frame("error", {"error": "Workflow finished without calling a Respond to Webhook node", "execution_id": execution_id})
                        return
                    yield _sse_frame("done", {
                        "status": result_status,
                        "execution_id": execution_id,
                        "session_id": msg.session_id,
                        "reply": _extract_reply(last_output),
                        "output": last_output,
                    })
                    return
        finally:
            pump_task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
