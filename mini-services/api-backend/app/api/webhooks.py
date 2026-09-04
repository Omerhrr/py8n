"""Public webhook catcher (Phase 4).

POST/GET /api/v1/webhooks/{workflow_id} - validates the workflow has an active
webhook_trigger node, injects the request envelope into the execution context
and dispatches asynchronously (or awaits the last node when configured).
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Workflow
from ..services.dispatcher import dispatch_execution
from ..services.events import get_event_bus
from ..services.executor import _background_tasks, execute_workflow
from ..services.webhook_info import public_webhook_url
from ._ratelimit import rate_limit

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Audit hardening: headers persisted with the webhook envelope must never
# carry credentials. Exact names cover the classic auth channels; the
# substring rule catches look-alikes (x-auth-token, x-sentry-key, ...
# anything containing token/secret/password/key).
_REDACTED = "[REDACTED]"
_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}
_SENSITIVE_NAME_PARTS = ("token", "secret", "password", "key")


def _redact_headers(headers) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for name, value in headers:
        lname = name.lower()
        if lname in _SENSITIVE_HEADERS or any(part in lname for part in _SENSITIVE_NAME_PARTS):
            redacted[name] = _REDACTED
        else:
            redacted[name] = value
    return dict(list(redacted.items())[:40])  # keep the v4 40-header safety valve


async def _read_body_capped(request: Request) -> bytes:
    """Read the raw body with a hard size cap (audit hardening).

    Rejects early on a lying-large Content-Length, and also guards the
    streaming read so chunked/undeclared bodies cannot balloon memory.
    """
    cap = settings.max_webhook_body_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise HTTPException(
            status_code=413,
            detail=f"Webhook body too large (limit {cap} bytes)",
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise HTTPException(
                status_code=413,
                detail=f"Webhook body too large (limit {cap} bytes)",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass
class WebhookResponder:
    """Response channel handed to the run's respond_to_webhook node (v21).

    The node calls it with (status_code, body, content_type); the first call
    wins and releases the waiting HTTP request via the asyncio event.
    """

    event: asyncio.Event = field(default_factory=asyncio.Event)
    status: int = 200
    body: object = None
    content_type: str = "application/json"
    responded: bool = False

    async def __call__(self, status_code: int, body: object, content_type: str) -> None:
        if self.responded:
            return  # first respond wins; later calls are no-ops
        self.status = status_code
        self.body = body
        self.content_type = content_type
        self.responded = True
        self.event.set()


async def _load_webhook_workflow(workflow_id: str, db: AsyncSession) -> Workflow:
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Unknown webhook (workflow not found)")
    if not wf.is_active:
        raise HTTPException(status_code=409, detail="Workflow is inactive - activate it to enable its webhook")
    if not wf.webhook_nodes():
        raise HTTPException(status_code=409, detail="Workflow has no Webhook Trigger node")
    return wf


def _request_envelope(request: Request, body: object) -> dict:
    return {
        "method": request.method,
        "headers": _redact_headers(request.headers.items()),
        "query": dict(request.query_params),
        "body": body,
    }


def _enforce_webhook_auth(request: Request, params: dict) -> None:
    """v23: reject unauthenticated webhook calls BEFORE the flow runs.

    Timing-safe comparisons (hmac.compare_digest) for both modes.
    """
    mode = (params.get("auth_mode") or "none").lower()
    if mode == "none":
        return

    if mode == "header":
        name = (params.get("auth_header_name") or "X-Webhook-Token").strip()
        expected = str(params.get("auth_header_value") or "")
        provided = request.headers.get(name, "")
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Invalid or missing auth header")
        return

    if mode == "basic":
        user = str(params.get("auth_user") or "")
        password = str(params.get("auth_pass") or "")
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            raise HTTPException(status_code=401, detail="Basic auth required")
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            provided_user, _, provided_pass = decoded.partition(":")
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=401, detail="Malformed Basic auth header")
        ok = hmac.compare_digest(provided_user, user) and hmac.compare_digest(provided_pass, password)
        if not ok:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return

    raise HTTPException(status_code=401, detail=f"Unknown auth mode {mode!r}")


@router.api_route(
    "/{workflow_id}",
    methods=["POST", "GET", "PUT", "PATCH", "DELETE"],
    tags=["webhooks"],
    dependencies=[Depends(rate_limit("webhook"))],
)
async def catch_webhook(workflow_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    wf = await _load_webhook_workflow(workflow_id, db)
    body: object = None
    if request.method in ("POST", "PUT", "PATCH"):
        raw = await _read_body_capped(request)
        try:
            body = json.loads(raw) if raw else None
        except (ValueError, UnicodeDecodeError):  # noqa: BLE001 - same contract as request.json()
            body = None

    node = wf.webhook_nodes()[0]
    params = node.get("parameters") or {}
    _enforce_webhook_auth(request, params)  # v23: 401 before the flow runs
    # v68: serving tokens - a deployment-backed workflow with >=1 active
    # token demands it (Bearer / X-Deployment-Token) before the flow runs.
    try:
        from ..services.deployments import check_serving_auth

        await check_serving_auth(db, workflow_id, request)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response_mode = params.get("response_mode", "immediately")
    envelope = _request_envelope(request, body)

    if response_mode == "respond_node":
        # v21: run the flow in the background and wait until EITHER a
        # respond_to_webhook node answers, the flow finishes without answering,
        # or the wait limit expires. After responding, the flow keeps running.
        responder = WebhookResponder()
        flow_task = asyncio.create_task(
            execute_workflow(
                workflow_id,
                trigger_type="webhook",
                trigger_payload=envelope,
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
            # Custom answer - the flow (if still running) continues in background.
            if responder.content_type == "application/json":
                return JSONResponse(content=responder.body, status_code=responder.status)
            return PlainTextResponse(
                str(responder.body), status_code=responder.status, media_type="text/plain"
            )

        waiter.cancel()
        if flow_task in done:
            # Flow ended without answering.
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
