"""The interaction layer (v68) - the system underneath the integrations.

Thesis: py8n doesn't become a collection of channel integrations; it is
the layer the channels plug into. A conversation is the unit of
continuity - one participant, one context, one AI handler - and channels
(voice, whatsapp, telegram, discord, web, app, api, sms, email) are
INTERCHANGEABLE ADAPTERS that deliver messages into and out of it.

The contract every adapter gets:

* inbound:  ``POST /interactions/inbound`` {channel, sender, text,
  conversation_ref?, handler_workflow_id?} -> find-or-create the
  conversation, record the message, run the bound handler workflow, and
  return its reply. A provider adapter (Twilio webhook relay, Meta cloud
  webhook, Telegram bot relay, Discord bot, your own app) translates its
  platform's events into THIS shape; the business logic never sees the
  channel.
* continuity: pass ``conversation_ref`` and the conversation survives a
  channel hop - the transcript records which channel each message used,
  so "called, no answer, followed up on WhatsApp" is ONE conversation.
* handler: any workflow can answer. The convention is the LAST node's
  output supplies the reply (keys tried in order: text / reply /
  generated / message / output / content, else str(output)) - an
  ``ai_agent`` node or a plain template node both fit.
* human takeover: messages with role ``human_agent`` are recorded like
  any other and the handler keeps running unless the conversation is
  closed - escalation is a state change, not a new architecture.

Channel CATALOG is a constant (derived, nothing stored): builtin
channels (app/web/api) deliver in-platform end to end; external ones
name their known providers and honestly report what an adapter must
supply. The sandbox records every outbound message and returns it to
the caller - last-mile provider delivery is the adapter's job, and the
conversation layer is identical either way.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import InteractionConversation, InteractionMessage, Workflow

# The channel catalog - adapters are metadata, the conversation is the product.
CHANNEL_CATALOG: dict[str, dict] = {
    "app": {
        "label": "In-app", "builtin": True,
        "description": "Chat inside an app py8n built - the AI employee lives in your own product.",
        "adapter": {"inbound": "in-process / universal ingress", "outbound": "echo (returned + recorded)"},
    },
    "web": {
        "label": "Web chat", "builtin": True,
        "description": "Website chat widget posting to the universal ingress.",
        "adapter": {"inbound": "universal ingress", "outbound": "echo (returned + recorded)"},
    },
    "api": {
        "label": "API", "builtin": True,
        "description": "Plain HTTP callers - machines are participants too.",
        "adapter": {"inbound": "universal ingress", "outbound": "echo (returned + recorded)"},
    },
    "voice": {
        "label": "Voice / Phone", "builtin": False,
        "description": "Programmatic phone calls: dial, answer, speech<->AI<->speech, barge-in, hangup.",
        "providers": ["twilio", "telnyx", "sip_trunk", "plivo"],
        "adapter": {"inbound": "provider webhook -> universal ingress",
                    "outbound": "provider API via credential (adapter's job)",
                    "extras": ["call status", "recording", "voicemail detection"]},
    },
    "whatsapp": {
        "label": "WhatsApp", "builtin": False,
        "description": "WhatsApp Business messaging through Meta's Cloud API.",
        "providers": ["meta_cloud_api", "webhook_relay"],
        "adapter": {"inbound": "Meta webhook -> universal ingress",
                    "outbound": "Graph API via credential (adapter's job)"},
    },
    "telegram": {
        "label": "Telegram", "builtin": False,
        "description": "Telegram bot conversations via the Bot API webhook.",
        "providers": ["bot_api_webhook"],
        "adapter": {"inbound": "bot webhook -> universal ingress",
                    "outbound": "Bot API via credential (adapter's job)"},
    },
    "discord": {
        "label": "Discord", "builtin": False,
        "description": "Discord bots answering in channels and DMs.",
        "providers": ["bot_webhook", "gateway_relay"],
        "adapter": {"inbound": "bot events -> universal ingress",
                    "outbound": "bot client via credential (adapter's job)"},
    },
    "sms": {
        "label": "SMS", "builtin": False,
        "description": "Plain text messaging - the retry lane when calls go unanswered.",
        "providers": ["twilio", "telnyx", "messagebird"],
        "adapter": {"inbound": "provider webhook -> universal ingress",
                    "outbound": "provider API via credential (adapter's job)"},
    },
    "email": {
        "label": "Email", "builtin": False,
        "description": "Inbound parse webhooks and SMTP send - the long-form channel.",
        "providers": ["smtp_imap", "sendgrid", "provider_webhook"],
        "adapter": {"inbound": "parse webhook -> universal ingress",
                    "outbound": "SMTP / provider API via credential (adapter's job)"},
    },
}

ROLES = ("user", "agent", "human_agent", "system")
_REPLY_KEYS = ("text", "reply", "generated", "message", "output", "content")


class InteractionError(ValueError):
    """Raised for honest 4xx-grade interaction failures."""


def channel_out(ch_id: str, meta: dict, conversations: int = 0) -> dict:
    return {
        "id": ch_id,
        "label": meta["label"],
        "builtin": meta["builtin"],
        "description": meta.get("description", ""),
        "providers": meta.get("providers", []),
        "adapter": meta.get("adapter", {}),
        "conversations": conversations,
    }


async def channel_matrix(db: AsyncSession, owner_id: str | None) -> list[dict]:
    """The adapter catalog + derived usage counts (never stored)."""
    q = select(InteractionConversation.channel)
    if owner_id is not None:
        q = q.where(InteractionConversation.owner_id == owner_id)
    counts: dict[str, int] = {}
    for (ch,) in (await db.execute(q)).all():
        counts[ch] = counts.get(ch, 0) + 1
    return [channel_out(cid, meta, counts.get(cid, 0)) for cid, meta in CHANNEL_CATALOG.items()]


def _extract_reply(output: object) -> str:
    """The handler convention: the LAST node's output supplies the reply.

    Understands the code node's standard ``{"result": ...}`` wrapper (a
    code node is the offline-friendly handler, so its output shape is
    unwrapped before the reply keys are tried).
    """
    if output is None:
        return ""
    if isinstance(output, dict):
        if set(output.keys()) == {"result"}:
            return _extract_reply(output["result"])
        for key in _REPLY_KEYS:
            if key in output and output[key] is not None:
                return str(output[key])
        return str(output)
    if isinstance(output, list):
        # split/split_out style outputs: prefer the first dict's reply keys
        for item in output:
            if isinstance(item, dict):
                for key in _REPLY_KEYS:
                    if key in item and item[key] is not None:
                        return str(item[key])
        return str(output)
    return str(output)


def message_out(row: InteractionMessage) -> dict:
    return {
        "id": row.id,
        "role": row.role,
        "channel": row.channel,
        "text": row.text,
        "payload": row.payload or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def conversation_out(row: InteractionConversation, messages: list[dict] | None = None,
                     handler_name: str | None = None, channels_used: list[str] | None = None,
                     message_count: int = 0) -> dict:
    return {
        "id": row.id,
        "channel": row.channel,
        "participant": {"id": row.participant_id, "name": row.participant_name},
        "state": row.state,
        "outcome": row.outcome,
        "context": row.context or {},
        "handler_workflow_id": row.handler_workflow_id,
        "handler_workflow_name": handler_name,
        "channels_used": channels_used or [row.channel],
        "message_count": message_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
        "messages": messages,
    }


async def _load_messages(db: AsyncSession, conversation_id: str) -> list[InteractionMessage]:
    q = (select(InteractionMessage)
         .where(InteractionMessage.conversation_id == conversation_id)
         .order_by(InteractionMessage.created_at.asc(), InteractionMessage.id.asc()))
    return list((await db.execute(q)).scalars().all())


async def _handler_name(db: AsyncSession, workflow_id: str | None) -> str | None:
    if not workflow_id:
        return None
    wf = await db.get(Workflow, workflow_id)
    return wf.name if wf else None


async def get_conversation(db: AsyncSession, conversation_id: str,
                           owner_id: str | None) -> dict | None:
    row = await db.get(InteractionConversation, conversation_id)
    if row is None:
        return None
    if owner_id is not None and row.owner_id is not None and row.owner_id != owner_id:
        return None
    msgs = await _load_messages(db, row.id)
    return conversation_out(
        row, [message_out(m) for m in msgs],
        handler_name=await _handler_name(db, row.handler_workflow_id),
        channels_used=sorted({m.channel for m in msgs if m.channel} or {row.channel}),
        message_count=len(msgs))


async def list_conversations(db: AsyncSession, owner_id: str | None,
                             channel: str | None = None, state: str | None = None,
                             limit: int = 100) -> list[dict]:
    q = select(InteractionConversation).order_by(
        InteractionConversation.last_message_at.desc().nullslast(),
        InteractionConversation.created_at.desc())
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    if channel:
        rows = [r for r in rows if r.channel == channel]
    if state:
        rows = [r for r in rows if r.state == state]
    out = []
    for r in rows[: max(1, min(limit, 200))]:
        msgs = await _load_messages(db, r.id)
        out.append(conversation_out(
            r, None, handler_name=await _handler_name(db, r.handler_workflow_id),
            channels_used=sorted({m.channel for m in msgs if m.channel} or {r.channel}),
            message_count=len(msgs)))
    return out


async def create_conversation(db: AsyncSession, *, owner_id: str | None, channel: str,
                              participant_id: str = "", participant_name: str = "",
                              handler_workflow_id: str | None = None,
                              context: dict | None = None) -> dict:
    """Open a conversation. The handler may also be bound later."""
    if channel not in CHANNEL_CATALOG:
        raise InteractionError(
            f"unknown channel {channel!r} - known channels: {', '.join(sorted(CHANNEL_CATALOG))}")
    if handler_workflow_id:
        wf = await db.get(Workflow, handler_workflow_id)
        if wf is None or (owner_id is not None and wf.owner_id is not None and wf.owner_id != owner_id):
            raise InteractionError(f"handler workflow {handler_workflow_id!r} not found")
    row = InteractionConversation(
        channel=channel,
        participant_id=(participant_id or "")[:180],
        participant_name=(participant_name or "")[:180],
        handler_workflow_id=handler_workflow_id,
        context=context or {},
    )
    row.owner_id = owner_id
    db.add(row)
    await db.flush()
    await db.refresh(row)
    db.add(InteractionMessage(
        conversation_id=row.id, role="system", channel=channel,
        text=f"conversation opened on {channel}",
        payload={"participant_id": row.participant_id},
    ))
    await db.flush()
    return await get_conversation(db, row.id, owner_id)  # type: ignore[return-value]


async def _find_or_create(db: AsyncSession, *, owner_id: str | None, channel: str,
                          participant_id: str, participant_name: str,
                          conversation_ref: str | None,
                          handler_workflow_id: str | None) -> InteractionConversation:
    if conversation_ref:
        row = await db.get(InteractionConversation, conversation_ref)
        if row is None or (owner_id is not None and row.owner_id is not None
                           and row.owner_id != owner_id):
            raise InteractionError(f"conversation_ref {conversation_ref!r} not found")
        return row
    q = (select(InteractionConversation)
         .where(InteractionConversation.owner_id == owner_id,
                InteractionConversation.channel == channel,
                InteractionConversation.participant_id == participant_id,
                InteractionConversation.state == "open")
         .order_by(InteractionConversation.created_at.desc()))
    row = (await db.execute(q)).scalars().first()
    if row is not None:
        return row
    created = await create_conversation(
        db, owner_id=owner_id, channel=channel, participant_id=participant_id,
        participant_name=participant_name, handler_workflow_id=handler_workflow_id)
    return await db.get(InteractionConversation, created["id"])  # type: ignore[return-value]


async def _run_handler(db: AsyncSession, conv: InteractionConversation, text: str,
                       channel: str, metadata: dict) -> str:
    """Dispatch the bound workflow and read the reply off the last node."""
    if not conv.handler_workflow_id:
        return ""
    wf = await db.get(Workflow, conv.handler_workflow_id)
    if wf is None:
        raise InteractionError("the bound handler workflow no longer exists")
    history = await _load_messages(db, conv.id)
    tail = [{"role": m.role, "channel": m.channel, "text": m.text} for m in history[-10:]]
    envelope = {
        "conversation_id": conv.id,
        "channel": channel,
        "participant": {"id": conv.participant_id, "name": conv.participant_name},
        "text": text,
        "history": tail,
        "metadata": metadata or {},
    }
    from .executor import execute_workflow

    # The manual trigger wraps run payloads under "payload" - the envelope
    # rides inside so `input_data["payload"]` is the conversation context.
    result = await execute_workflow(
        conv.handler_workflow_id, trigger_type="webhook",
        trigger_payload={"payload": envelope}, trigger_node_id=None)
    if result.get("status") != "success":
        raise InteractionError(
            f"handler workflow failed: {result.get('error') or result.get('status')}")
    last_output = result["node_runs"][-1].get("output") if result.get("node_runs") else None
    return _extract_reply(last_output)


async def ingest(db: AsyncSession, *, owner_id: str | None, channel: str,
                 sender_id: str = "", sender_name: str = "", text: str,
                 conversation_ref: str | None = None,
                 handler_workflow_id: str | None = None,
                 metadata: dict | None = None) -> dict:
    """The universal adapter ingress - ANY channel, ONE conversation layer."""
    if channel not in CHANNEL_CATALOG:
        raise InteractionError(
            f"unknown channel {channel!r} - known channels: {', '.join(sorted(CHANNEL_CATALOG))}")
    text = str(text or "").strip()
    if not text:
        raise InteractionError("inbound text is required")
    conv = await _find_or_create(
        db, owner_id=owner_id, channel=channel,
        participant_id=(sender_id or "")[:180], participant_name=(sender_name or "")[:180],
        conversation_ref=conversation_ref, handler_workflow_id=handler_workflow_id)
    if handler_workflow_id and not conv.handler_workflow_id:
        conv.handler_workflow_id = handler_workflow_id
        db.add(conv)
    db.add(InteractionMessage(conversation_id=conv.id, role="user",
                              channel=channel, text=text[:20000],
                              payload=metadata or {}))
    await db.flush()
    # Commit BEFORE dispatching the handler: the flow runs on separate
    # sessions and SQLite allows a single writer - holding this request's
    # write transaction across execute_workflow deadlocks the database.
    await db.commit()

    reply = await _run_handler(db, conv, text, channel, metadata or {})
    if reply:
        db.add(InteractionMessage(conversation_id=conv.id, role="agent",
                                  channel=channel, text=reply[:20000],
                                  payload={"via": "handler_workflow",
                                           "handler_workflow_id": conv.handler_workflow_id}))
    conv.last_message_at = datetime.now(timezone.utc)
    db.add(conv)
    await db.flush()
    return {
        "conversation_id": conv.id,
        "channel": channel,
        "delivery": "echo",
        "reply": reply or None,
        "handler_bound": bool(conv.handler_workflow_id),
        "state": conv.state,
    }


async def send_message(db: AsyncSession, conv: InteractionConversation, *, role: str,
                       text: str, channel: str | None = None) -> dict:
    """Record a message from any role. user -> runs the handler (same as ingest)."""
    if role not in ROLES:
        raise InteractionError(f"role must be one of {', '.join(ROLES)}")
    text = str(text or "").strip()
    if not text:
        raise InteractionError("message text is required")
    ch = channel or conv.channel
    db.add(InteractionMessage(conversation_id=conv.id, role=role, channel=ch,
                              text=text[:20000], payload={}))
    reply = None
    if role == "user":
        # same single-writer discipline as ingest: commit before the flow runs
        await db.commit()
        reply = await _run_handler(db, conv, text, ch, {}) or None
        if reply:
            db.add(InteractionMessage(conversation_id=conv.id, role="agent",
                                      channel=ch, text=reply[:20000],
                                      payload={"via": "handler_workflow"}))
    conv.last_message_at = datetime.now(timezone.utc)
    db.add(conv)
    await db.flush()
    return {"conversation_id": conv.id, "role": role, "channel": ch,
            "reply": reply, "state": conv.state}


async def close_conversation(db: AsyncSession, conv: InteractionConversation,
                             outcome: str = "") -> dict:
    conv.state = "closed"
    conv.outcome = (outcome or "").strip()[:180]
    conv.last_message_at = datetime.now(timezone.utc)
    db.add(conv)
    db.add(InteractionMessage(
        conversation_id=conv.id, role="system", channel=conv.channel,
        text=f"conversation closed" + (f" - outcome: {conv.outcome}" if conv.outcome else ""),
        payload={"outcome": conv.outcome}))
    await db.flush()
    return {"id": conv.id, "state": conv.state, "outcome": conv.outcome}


async def bind_handler(db: AsyncSession, conv: InteractionConversation,
                       workflow_id: str | None, owner_id: str | None) -> dict:
    """Bind (or unbind with None) the workflow that answers this conversation."""
    if workflow_id:
        wf = await db.get(Workflow, workflow_id)
        if wf is None or (owner_id is not None and wf.owner_id is not None and wf.owner_id != owner_id):
            raise InteractionError(f"handler workflow {workflow_id!r} not found")
    conv.handler_workflow_id = workflow_id
    db.add(conv)
    db.add(InteractionMessage(
        conversation_id=conv.id, role="system", channel=conv.channel,
        text=(f"handler bound: {wf.name}" if workflow_id else "handler unbound"),
        payload={"handler_workflow_id": workflow_id}))
    await db.flush()
    return {"id": conv.id, "handler_workflow_id": conv.handler_workflow_id,
            "handler_workflow_name": await _handler_name(db, workflow_id)}
