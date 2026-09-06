"""Callbacks instead of hold (v78) - the queue and the dialer, composed.

v76 queued live calls; v77 made the wait spoken and texted. But the
deepest truth about waiting is that the best wait is NO wait: a caller
with a phone does not need to hold the line - they need their place in
line to CALL THEM BACK. This module is that composition, built entirely
from primitives the platform already owns:

* the ChannelQueue keeps the caller's ORIGINAL place (``joined_at``
  survives - a callback does not lose the line);
* the VoiceCampaign is the dialer: the queue COMPOSES one campaign per
  queue (found by the ``callback_for_queue`` marker in its config) bound
  to the queue's OWN agent, and books each callback request as a target
  on it - the campaign's whole machinery (honest skips without
  credentials, retry schedules, AMD, the answered-session path) applies
  to callbacks for free, because the callback IS a campaign target;
* when a callback dial is ANSWERED, the campaign's on_call_event opens
  the real session (as any campaign answer does) and this module's
  attach hook seats the answered call into the queue's destination
  meeting - the caller walks from "we'll call you back" into the room
  on the callback call itself.

The lifecycle of one callback:

    waiting --request_callback--> callback (the hold ENDS honestly:
        the call hangs up with reason=callback; a campaign target is
        booked; the entry keeps its joined_at)
    callback --dial_callbacks--> campaign dials (pending -> dialing)
    answered --> on_callback_answered attaches the session to the
        queue's destination meeting and books the answer on the entry;
    or no_answer/failed -> the campaign's own retry schedule drives the
        re-dial (the queue reads the picture from the campaign).

Honesty: the promise "we will call you back" is TRAFFIC STATE (the
target row) - you cannot derive that a promise was made. Positions,
however, stay derived: the entry's original joined_at keeps its place,
nothing about order is stored twice.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChannelEndpoint, ChannelQueue, ChannelQueueEntry, VoiceCampaign, VoiceSession
from .voice_campaigns import VoiceCampaignError, _load as _load_campaign
from .voice_queue import VoiceQueueError, _load as _load_queue

# the entry status that means "off the line, waiting for the dialer"
CALLBACK_STATUS = "callback"


def _now():
    return datetime.now(timezone.utc)


def callback_config(raw: dict | None) -> dict:
    """The queue's callback block, validated at CONFIG WRITE time.

    ``{enabled (default false), endpoint_id}`` - enabled is the promise
    the line can be left; endpoint_id is the telnyx voice endpoint the
    callback dials THROUGH (bindable later - dial_callbacks reads the
    queue's CURRENT binding every pass, the queue is the source of
    truth, and a missing endpoint produces the campaign's honest
    skipped-with-reason records, never a silent nothing)."""
    raw = dict((raw or {}).get("callback") or {})
    return {"enabled": bool(raw.get("enabled", False)),
            "endpoint_id": str(raw.get("endpoint_id") or "").strip()[:36] or ""}


async def callback_campaign(db: AsyncSession, queue: ChannelQueue, *,
                            create: bool = False) -> VoiceCampaign | None:
    """The queue's composed callback campaign (found by marker), created
    on the first callback request when ``create`` - bound to the queue's
    OWN agent (a callback nobody answers through the line's persona is
    just a strange phone call)."""
    q = (select(VoiceCampaign)
         .where(VoiceCampaign.config["callback_for_queue"].as_string() == queue.id)
         .order_by(VoiceCampaign.created_at.asc()))
    row = (await db.execute(q)).scalars().first()
    if row is not None or not create:
        return row
    if not queue.agent_id:
        raise VoiceQueueError(
            "callbacks dial through an agent - bind agent_id on the queue first")
    from ..models import VoiceCampaign as VoiceCampaignRow
    from .voice_campaigns import validate_config

    cfg = callback_config(queue.config)
    # composed EMPTY on purpose: the targets are the callbacks as they are
    # requested (create_campaign refuses empty lists - a campaign built to
    # RECEIVE targets is composed here, not through the list-dialer path)
    row = VoiceCampaignRow(
        owner_id=queue.owner_id, agent_id=queue.agent_id,
        name=f"callback: {queue.name}"[:140],
        endpoint_id=cfg["endpoint_id"] or None,
        config=validate_config({"callback_for_queue": queue.id}))
    db.add(row)
    await db.flush()
    return row


async def request_callback(db: AsyncSession, owner_id: str | None, queue_id: str,
                           entry_id: str) -> dict:
    """Trade the hold for a callback: the caller's place in line is kept
    (joined_at survives), the HOLD ends honestly (the call hangs up with
    reason=callback - nobody stays on the line), and a target is booked
    on the queue's composed campaign. The remaining waiting callers hear
    the line move (the v77 passes run)."""
    row = await _load_queue(db, queue_id, owner_id)
    cfg = callback_config(row.config)
    if not cfg["enabled"]:
        raise VoiceQueueError(
            "this queue does not take callbacks - enable config.callback.enabled")
    entry = await db.get(ChannelQueueEntry, entry_id)
    if entry is None or entry.queue_id != row.id:
        raise VoiceQueueError(f"queue entry {entry_id!r} not found in queue {queue_id!r}")
    if entry.status != "waiting":
        raise VoiceQueueError(f"the entry is already {entry.status} - callbacks are "
                              "traded from waiting, not from history")
    session = await db.get(VoiceSession, entry.session_id) if entry.session_id else None
    campaign = await callback_campaign(db, row, create=True)
    if campaign is None:  # pragma: no cover - create=True always returns a row
        raise VoiceQueueError("the callback campaign could not be composed")
    from .voice_campaigns import VoiceCampaignTarget

    # book the target FIRST (fail loud before the caller's call is ended)
    target = VoiceCampaignTarget(
        campaign_id=campaign.id, owner_id=row.owner_id,
        address=entry.address or "", name=entry.label or "",
        meta={"queue_id": row.id, "queue_entry_id": entry.id,
              "callback_requested_at": _now().isoformat()})
    db.add(target)
    await db.flush()
    # end the hold: the caller hangs up, their place in line does not
    if session is not None and session.state in ("on_hold", "in_progress"):
        from . import voice as voice_svc

        await voice_svc.hangup(db, session, reason="callback")
    elif session is not None and session.state == "ended":
        pass  # the caller already hung up - the callback is still valid
    entry.status = CALLBACK_STATUS
    entry.left_at = _now()
    entry.meta = {**(entry.meta or {}),
                  "callback": {"requested_at": _now().isoformat(),
                               "campaign_id": campaign.id, "target_id": target.id}}
    db.add(entry)
    await db.flush()
    from .voice_queue import _waiting_hooks

    hooks = await _waiting_hooks(db, row, moved=True)
    out = {"entry_id": entry.id, "campaign_id": campaign.id,
           "target_id": target.id, "address": target.address,
           "note": ("the hold ended (reason=callback) and a callback target was "
                    "booked on the queue's campaign - dial_callbacks (or the "
                    "campaign's own start/retry pass) places the call; the "
                    "entry keeps its original place in line"),
           }
    out.update(hooks)
    return out


async def dial_callbacks(db: AsyncSession, owner_id: str | None, queue_id: str,
                         *, limit: int | None = None, sender=None) -> dict:
    """Place the callback dials: the composed campaign's start pass, in
    callback order (targets are booked in request order; the entry's
    original joined_at survives for anyone still holding). Delegates to
    the campaign machinery ON PURPOSE - honest skips without credentials,
    retry schedules, AMD and the answered path all apply unchanged."""
    row = await _load_queue(db, queue_id, owner_id)
    cfg = callback_config(row.config)
    if not cfg["enabled"]:
        raise VoiceQueueError(
            "this queue does not take callbacks - enable config.callback.enabled")
    campaign = await callback_campaign(db, row)
    if campaign is None:
        raise VoiceQueueError("no callbacks have been requested yet - the callback "
                              "campaign is composed on the first request")
    if campaign.status in ("stopped", "completed"):
        raise VoiceQueueError(f"the callback campaign is {campaign.status} - "
                              "no further callback dials")
    # the queue is the source of truth for the dialing endpoint
    if cfg["endpoint_id"] and campaign.endpoint_id != cfg["endpoint_id"]:
        endpoint = await db.get(ChannelEndpoint, cfg["endpoint_id"])
        if endpoint is None or (row.owner_id is not None
                                and endpoint.owner_id is not None
                                and endpoint.owner_id != row.owner_id):
            raise VoiceQueueError(
                f"callback endpoint {cfg['endpoint_id']!r} not found")
        campaign.endpoint_id = cfg["endpoint_id"]
        db.add(campaign)
        await db.flush()
    from .voice_campaigns import campaign_out, start_campaign

    out = await start_campaign(db, owner_id, campaign.id, limit=limit, sender=sender)
    out["queue_id"] = row.id
    out["note"] = ("the callback dials went through the SAME campaign machinery as "
                   "any outbound dial - skipped honestly without credentials, "
                   "answered calls attach to the queue's destination meeting")
    return out


async def callbacks_picture(db: AsyncSession, queue: ChannelQueue) -> dict:
    """The callback picture of one queue, derived from the composed
    campaign's targets at read time (detail view only - one extra query
    per queue keeps the LIST view cheap)."""
    campaign = await callback_campaign(db, queue)
    if campaign is None:
        return {"campaign_id": None, "requested": 0}
    from .voice_campaigns import _targets as _campaign_targets

    legs = await _campaign_targets(db, campaign.id)
    counts: dict[str, int] = {}
    for t in legs:
        counts[t.status] = counts.get(t.status, 0) + 1
    return {"campaign_id": campaign.id, "campaign_status": campaign.status,
            "requested": len(legs),
            "counts": counts,
            "due": sum(1 for t in legs if t.status == "pending")}


async def on_callback_answered(db: AsyncSession, campaign: VoiceCampaign,
                               target, session: VoiceSession) -> dict | None:
    """The answer hook (called from the campaign's on_call_event AND its
    simulate path): a callback dial was ANSWERED - seat the answered call
    into the queue's destination meeting (the caller walks into the room
    on the callback call) and book the answer on the entry. Best-effort
    by contract: a queue without a destination meeting still had its
    callback answered (the agent talks), only the seating is absent."""
    queue_id = str((campaign.config or {}).get("callback_for_queue") or "")
    if not queue_id:
        return None
    queue = await db.get(ChannelQueue, queue_id)
    if queue is None:
        return None
    meta = dict(target.meta or {})
    entry_id = str(meta.get("queue_entry_id") or "")
    attached = None
    if queue.meeting_id and session.state != "ended":
        try:
            from .voice_meetings import attach_session

            attach = await attach_session(db, queue.owner_id, queue.meeting_id,
                                          session.id,
                                          label=target.name or target.address)
            attached = {"meeting_id": attach["meeting_id"],
                        "participant_id": attach["participant"]["id"]}
        except Exception as exc:  # noqa: BLE001 - a full/ended room must not
            # break the ANSWER (the agent still talks on the callback)
            attached = {"error": str(exc)[:300]}
    entry = await db.get(ChannelQueueEntry, entry_id) if entry_id else None
    if entry is not None:
        cb = dict((entry.meta or {}).get("callback") or {})
        cb.update({"answered_at": _now().isoformat(),
                   "session_id": session.id, **({"attached": attached}
                                                if attached else {})})
        entry.meta = {**(entry.meta or {}), "callback": cb}
        db.add(entry)
    tmeta = dict(target.meta or {})
    tmeta["callback_answered"] = {"at": _now().isoformat(), **(attached or {})}
    target.meta = tmeta
    db.add(target)
    await db.flush()
    return {"queue_id": queue.id, "entry_id": entry_id or None,
            "attached": attached, "session_id": session.id}


async def queue_for_campaign(db: AsyncSession, campaign_id: str,
                             owner_id: str | None) -> ChannelQueue | None:
    """The queue a callback campaign composes (used by the campaign view
    to name its composition honestly)."""
    campaign = await _load_campaign(db, campaign_id, owner_id)
    queue_id = str((campaign.config or {}).get("callback_for_queue") or "")
    if not queue_id:
        return None
    queue = await db.get(ChannelQueue, queue_id)
    if queue is None or (owner_id is not None and queue.owner_id is not None
                         and queue.owner_id != owner_id):
        return None
    return queue
