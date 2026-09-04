"""Channels API (v69) - the REAL provider adapter surface.

Management (owner-scoped, enforced auth):

* ``POST   /channels/endpoints``                     - register a provider
  connection (meta_cloud_api | telegram_bot_api | discord_bot) with the
  keys that VERIFY the provider's webhook + the credentials that DELIVER
  outbound; secrets are masked in every response
* ``GET    /channels/endpoints``                     - the estate
* ``GET    /channels/endpoints/{id}``                - one, with usage
* ``PUT    /channels/endpoints/{id}``                - config/handler/enabled
* ``DELETE /channels/endpoints/{id}``                - remove (transcripts survive)
* ``POST   /channels/endpoints/{id}/preview-outbound`` - dry-run: the exact
  request the provider's send API would receive, secrets masked
* ``POST   /channels/endpoints/{id}/deliver``        - actually send one
  message through the provider (honest delivery record)

Receiver endpoints (PUBLIC - providers cannot log in; each verifies its
own credentials before anything runs):

* ``GET/POST /channels/whatsapp/{endpoint_id}/webhook``  - Meta Cloud API:
  the GET handshake (hub.verify_token -> hub.challenge) and the POST with
  X-Hub-Signature-256 HMAC verification
* ``POST /channels/telegram/{endpoint_id}/webhook``      - Bot API updates,
  X-Telegram-Bot-Api-Secret-Token verified
* ``POST /channels/discord/{endpoint_id}/webhook``       - signed Discord
  interactions: Ed25519 verification, PING handshake answered

Every receiver funnels into the SAME receive path: verify -> parse the
native dialect -> interactions.ingest() per message -> deliver the reply
through the provider's send API. Status events (delivery receipts, edits)
are counted and reported as honest skips - they are not messages.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..models import ChannelEndpoint
from ..services import channel_adapters as adapters
from ..services import channel_endpoints as ep_svc
from ..services.channel_endpoints import ChannelEndpointError

router = APIRouter(prefix="/channels", tags=["channels"])


def _http(exc: ChannelEndpointError, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


async def _own_endpoint(db: AsyncSession, endpoint_id: str, user) -> ChannelEndpoint:
    row = await db.get(ChannelEndpoint, endpoint_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    if user is not None and row.owner_id is not None and row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    return row


# ---------------------------------------------------------------------------
# Endpoint management (enforced)
# ---------------------------------------------------------------------------


class EndpointCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=140)
    provider: str = Field(..., description="meta_cloud_api | telegram_bot_api | discord_bot")
    handler_workflow_id: str | None = Field(default=None, description="Workflow that answers (last node's output = the reply)")
    config: dict = Field(default_factory=dict, description="Provider secrets: verify_token/app_secret/phone_number_id (meta), secret_token/bot_token (telegram), public_key (discord)")


@router.get("/adapters")
async def adapters_catalog(user=Depends(get_optional_user)):
    """The provider adapter registry - what each provider needs and speaks."""
    return {"adapters": [
        {"id": pid, "channel": meta["channel"], "description": meta["description"],
         "secret_keys": meta["secret"], "credential_keys": meta["credential"],
         "webhook_path": f"/channels/{adapters.PROVIDER_PATHS[pid]}/{{endpoint_id}}/webhook"}
        for pid, meta in sorted(adapters.REQUIRED_CONFIG.items())]}


@router.post("/endpoints", status_code=201)
async def create_endpoint(body: EndpointCreate, user=Depends(get_optional_user),
                          db: AsyncSession = Depends(get_db)):
    try:
        return await ep_svc.create_endpoint(
            db, owner_id=getattr(user, "id", None), name=body.name,
            provider=body.provider.strip(), handler_workflow_id=body.handler_workflow_id,
            config=body.config)
    except ChannelEndpointError as exc:
        raise _http(exc) from exc


@router.get("/endpoints")
async def list_endpoints(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    return {"endpoints": await ep_svc.list_endpoints(db, getattr(user, "id", None))}


@router.get("/endpoints/{endpoint_id}")
async def get_endpoint(endpoint_id: str, user=Depends(get_optional_user),
                       db: AsyncSession = Depends(get_db)):
    out = await ep_svc.get_endpoint(db, endpoint_id, getattr(user, "id", None))
    if out is None:
        raise HTTPException(status_code=404, detail="Not found")
    return out


class EndpointUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=140)
    handler_workflow_id: str | None = None
    config: dict | None = None
    enabled: bool | None = None


@router.put("/endpoints/{endpoint_id}")
async def update_endpoint(endpoint_id: str, body: EndpointUpdate,
                          user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _own_endpoint(db, endpoint_id, user)
    try:
        return await ep_svc.update_endpoint(
            db, row, name=body.name, handler_workflow_id=body.handler_workflow_id,
            config=body.config, enabled=body.enabled)
    except ChannelEndpointError as exc:
        raise _http(exc) from exc


@router.delete("/endpoints/{endpoint_id}")
async def delete_endpoint(endpoint_id: str, user=Depends(get_optional_user),
                          db: AsyncSession = Depends(get_db)):
    row = await _own_endpoint(db, endpoint_id, user)
    return await ep_svc.delete_endpoint(db, row)


class OutboundBody(BaseModel):
    to: str = Field(..., min_length=1, max_length=180, description="Chat id / wa id / channel id / webhook URL")
    text: str = Field(..., min_length=1, max_length=4000)


@router.post("/endpoints/{endpoint_id}/preview-outbound")
async def preview_outbound(endpoint_id: str, body: OutboundBody,
                           user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _own_endpoint(db, endpoint_id, user)
    return await ep_svc.preview_outbound(row, body.to, body.text)


@router.post("/endpoints/{endpoint_id}/deliver")
async def deliver(endpoint_id: str, body: OutboundBody,
                  user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Actually send one message through the provider's send API."""
    row = await _own_endpoint(db, endpoint_id, user)
    if not row.enabled:
        raise HTTPException(status_code=400, detail="this endpoint is disabled")
    return await ep_svc.deliver_outbound(row, body.to, body.text)


# ---------------------------------------------------------------------------
# Provider webhook receivers (PUBLIC - verified by provider credentials)
# ---------------------------------------------------------------------------

async def _load_receiver(db: AsyncSession, endpoint_id: str) -> ChannelEndpoint:
    row = await db.get(ChannelEndpoint, endpoint_id)
    if row is None or row.provider not in adapters.REQUIRED_CONFIG:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.get("/whatsapp/{endpoint_id}/webhook")
async def whatsapp_verify(endpoint_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Meta's verification handshake: echo hub.challenge when the token matches."""
    row = await _load_receiver(db, endpoint_id)
    params = dict(request.query_params)
    ok, challenge = adapters.meta_verify_handshake(
        params, str((row.config or {}).get("verify_token") or ""))
    if not ok or challenge is None:
        raise HTTPException(status_code=403, detail="verification token mismatch")
    return PlainTextResponse(challenge, status_code=200)


@router.post("/whatsapp/{endpoint_id}/webhook")
async def whatsapp_webhook(endpoint_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    row = await _load_receiver(db, endpoint_id)
    raw = await request.body()
    try:
        return await ep_svc.receive_webhook(db, row, raw_body=raw,
                                            headers=dict(request.headers))
    except ChannelEndpointError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/telegram/{endpoint_id}/webhook")
async def telegram_webhook(endpoint_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    row = await _load_receiver(db, endpoint_id)
    raw = await request.body()
    try:
        out = await ep_svc.receive_webhook(db, row, raw_body=raw,
                                           headers=dict(request.headers))
        return JSONResponse({"ok": True, **out}, status_code=200)
    except ChannelEndpointError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.post("/discord/{endpoint_id}/webhook")
async def discord_webhook(endpoint_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    row = await _load_receiver(db, endpoint_id)
    raw = await request.body()
    # Discord PING must be answered even before full parse; signature first.
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if isinstance(payload, dict) and payload.get("type") == 1:
        ok, detail = adapters.verify_request("discord_bot", row.config or {},
                                             raw_body=raw, headers={
                                                 k.lower(): v for k, v in request.headers.items()})
        if not ok:
            raise HTTPException(status_code=401, detail=str(detail))
        return JSONResponse({"type": 1}, status_code=200)
    try:
        out = await ep_svc.receive_webhook(db, row, raw_body=raw,
                                           headers=dict(request.headers))
        # answer the interaction with the generated reply when there is one
        reply = next((h.get("reply") for h in out.get("handled", []) if h.get("reply")), None)
        if reply:
            return JSONResponse({"type": 4, "data": {"content": str(reply)[:2000]}},
                                status_code=200)
        return JSONResponse({"type": 5}, status_code=200)
    except ChannelEndpointError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
