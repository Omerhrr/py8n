"""Interactions API (v68) - the interaction layer: channels as adapters.

The conversation is the unit of continuity; channels are interchangeable
adapters. Every endpoint is owner-scoped and works identically no matter
which channel a participant used - that is the whole point.

* ``GET  /interactions/channels``                  - the adapter catalog
  (builtin channels + external ones with their providers and what an
  adapter must supply) + derived per-channel conversation counts
* ``POST /interactions/inbound``                   - the UNIVERSAL adapter
  ingress: any provider adapter (Twilio relay, Meta cloud webhook,
  Telegram bot, Discord bot, your own app) posts {channel, sender, text,
  conversation_ref?} here; py8n finds-or-creates the conversation, runs
  the bound handler workflow, records both messages, returns the reply
* ``GET  /interactions/conversations``             - list (channel/state filters)
* ``POST /interactions/conversations``             - open one explicitly
* ``GET  /interactions/conversations/{id}``        - full transcript with
  per-message channel stamps (the channel hops stay visible)
* ``POST /interactions/conversations/{id}/messages`` - send as any role;
  ``role: "user"`` continues the agent loop, ``human_agent`` is a takeover
* ``POST /interactions/conversations/{id}/handler`` - bind/unbind the
  answering workflow (last node's output = the reply)
* ``POST /interactions/conversations/{id}/close``   - close with an outcome
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..models import InteractionConversation
from ..services import interactions as inter_svc
from ..services.interactions import InteractionError

router = APIRouter(prefix="/interactions", tags=["interactions"])


def _http(exc: InteractionError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


async def _own_conversation(db: AsyncSession, conversation_id: str, user) -> InteractionConversation:
    row = await db.get(InteractionConversation, conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    if user is not None and row.owner_id is not None and row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.get("/channels")
async def channels(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    return {"channels": await inter_svc.channel_matrix(db, getattr(user, "id", None))}


class InboundBody(BaseModel):
    channel: str = Field(..., min_length=1, max_length=30,
                         description="app | web | api | voice | whatsapp | telegram | discord | sms | email")
    sender_id: str = Field(default="", max_length=180, description="Provider-side participant id (phone number, chat id, ...)")
    sender_name: str = Field(default="", max_length=180)
    text: str = Field(..., min_length=1, max_length=20000)
    conversation_ref: str | None = Field(default=None, description="Continue an existing conversation - survives channel hops")
    handler_workflow_id: str | None = Field(default=None, description="Workflow that answers (used when creating; last node's output = the reply)")
    metadata: dict = Field(default_factory=dict)


@router.post("/inbound")
async def inbound(body: InboundBody, user=Depends(get_optional_user),
                  db: AsyncSession = Depends(get_db)):
    """Universal adapter ingress - the single endpoint every channel posts to."""
    try:
        return await inter_svc.ingest(
            db, owner_id=getattr(user, "id", None), channel=body.channel,
            sender_id=body.sender_id, sender_name=body.sender_name, text=body.text,
            conversation_ref=body.conversation_ref,
            handler_workflow_id=body.handler_workflow_id, metadata=body.metadata)
    except InteractionError as exc:
        raise _http(exc) from exc


class ConversationCreate(BaseModel):
    channel: str = Field(..., min_length=1, max_length=30)
    participant_id: str = Field(default="", max_length=180)
    participant_name: str = Field(default="", max_length=180)
    handler_workflow_id: str | None = None
    context: dict = Field(default_factory=dict)


@router.get("/conversations")
async def list_conversations(channel: str | None = None, state: str | None = None,
                             limit: int = 100, user=Depends(get_optional_user),
                             db: AsyncSession = Depends(get_db)):
    return {"conversations": await inter_svc.list_conversations(
        db, getattr(user, "id", None), channel=channel, state=state, limit=limit)}


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationCreate, user=Depends(get_optional_user),
                              db: AsyncSession = Depends(get_db)):
    try:
        return await inter_svc.create_conversation(
            db, owner_id=getattr(user, "id", None), channel=body.channel,
            participant_id=body.participant_id, participant_name=body.participant_name,
            handler_workflow_id=body.handler_workflow_id, context=body.context)
    except InteractionError as exc:
        raise _http(exc) from exc


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, user=Depends(get_optional_user),
                           db: AsyncSession = Depends(get_db)):
    out = await inter_svc.get_conversation(db, conversation_id, getattr(user, "id", None))
    if out is None:
        raise HTTPException(status_code=404, detail="Not found")
    return out


class MessageBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)
    role: str = Field(default="user", description='user (runs the handler) | agent | human_agent | system')
    channel: str | None = Field(default=None, max_length=30, description="Override the channel stamp - the cross-channel hop")


@router.post("/conversations/{conversation_id}/messages")
async def send_message(conversation_id: str, body: MessageBody,
                       user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    conv = await _own_conversation(db, conversation_id, user)
    try:
        return await inter_svc.send_message(db, conv, role=body.role, text=body.text,
                                            channel=body.channel)
    except InteractionError as exc:
        raise _http(exc) from exc


class HandlerBody(BaseModel):
    workflow_id: str | None = Field(default=None, description="Workflow to bind; null unbinds")


@router.post("/conversations/{conversation_id}/handler")
async def bind_handler(conversation_id: str, body: HandlerBody,
                       user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    conv = await _own_conversation(db, conversation_id, user)
    try:
        return await inter_svc.bind_handler(db, conv, body.workflow_id,
                                            getattr(user, "id", None))
    except InteractionError as exc:
        raise _http(exc) from exc


class CloseBody(BaseModel):
    outcome: str = Field(default="", max_length=180)


@router.post("/conversations/{conversation_id}/close")
async def close_conversation(conversation_id: str, body: CloseBody,
                             user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    conv = await _own_conversation(db, conversation_id, user)
    return await inter_svc.close_conversation(db, conv, outcome=body.outcome)
