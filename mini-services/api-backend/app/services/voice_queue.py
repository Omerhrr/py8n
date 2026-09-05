"""Channel-side queueing and waiting (v76) - the waiting room as a primitive.

Every real phone system has the moment the user's request describes: more
callers than capacity. py8n's answer is the ChannelQueue - a FIRST-CLASS
waiting room on the channel side, built from primitives the platform
already owns:

* the caller WAITS in the session state machine's own ``on_hold`` state
  (the hold/unhold events from v69 - no new call states invented, the
  provider's media plane does whatever holding sounds like it does);
* the queue is FIFO by ``joined_at``; positions, waited-seconds and the
  longest wait are DERIVED at read time, never stored;
* a caller who hangs up while waiting is derived ABANDONED (the session
  ended - the truth is in the session row, the entry keeps its history);
* seating the head (POST /next) releases the call from hold and - when
  the queue has a destination meeting - ATTACHES the live session to the
  room as a participant leg (voice_meetings.attach_session), so the
  caller walks from the waiting room into the meeting ON THE SAME CALL;
* config: max_size (waiting capacity, fail loud when full) and
  max_wait_seconds (the SLA; breaches surface as ``expired: true`` on the
  entry - derived, honest, and never a silent drop).

Honesty: the queue holds CONVERSATIONS, not audio. It never pretends to
play hold music; the entry meta records exactly what py8n did (held the
session, released it, attached it to a room).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChannelQueue, ChannelQueueEntry, VoiceMeeting, VoiceSession
from . import voice as voice_svc


class VoiceQueueError(ValueError):
    """Honest 4xx-grade queue failures."""


DEFAULT_CONFIG = {"max_size": 20, "max_wait_seconds": 300}


def _now():
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive UTC datetimes - read them as UTC (the same
    normalization the meeting/analytic derivations use)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def queue_config(raw: dict | None) -> dict:
    raw = dict(raw or {})
    try:
        max_size = int(raw.get("max_size") or DEFAULT_CONFIG["max_size"])
    except (TypeError, ValueError):
        max_size = DEFAULT_CONFIG["max_size"]
    try:
        max_wait = int(raw.get("max_wait_seconds") or DEFAULT_CONFIG["max_wait_seconds"])
    except (TypeError, ValueError):
        max_wait = DEFAULT_CONFIG["max_wait_seconds"]
    return {"max_size": max(1, min(max_size, 200)),
            "max_wait_seconds": max(10, min(max_wait, 86400))}


async def _load(db: AsyncSession, queue_id: str, owner_id: str | None) -> ChannelQueue:
    row = await db.get(ChannelQueue, queue_id)
    if row is None:
        raise VoiceQueueError(f"queue {queue_id!r} not found")
    if owner_id is not None and row.owner_id is not None and row.owner_id != owner_id:
        raise VoiceQueueError(f"queue {queue_id!r} not found")
    return row


async def _entries(db: AsyncSession, queue_id: str) -> list[ChannelQueueEntry]:
    q = (select(ChannelQueueEntry)
         .where(ChannelQueueEntry.queue_id == queue_id)
         .order_by(ChannelQueueEntry.joined_at.asc(), ChannelQueueEntry.id.asc()))
    return list((await db.execute(q)).scalars().all())


def _entry_out(e: ChannelQueueEntry, session: VoiceSession | None, *,
               position: int | None, max_wait_seconds: int | None = None) -> dict:
    waited = None
    expired = False
    abandoned = False
    if e.status == "waiting":
        if session is not None and session.state == "ended":
            abandoned = True  # the caller hung up while waiting
        end = _aware(session.ended_at) if abandoned else _now()
        start = _aware(e.joined_at) or _now()
        waited = round(max(0.0, (end - start).total_seconds()), 3)
        expired = bool(max_wait_seconds and waited >= max_wait_seconds)
    return {"id": e.id, "queue_id": e.queue_id, "session_id": e.session_id or None,
            "label": e.label, "address": e.address, "status": e.status,
            "session_state": session.state if session is not None else None,
            "position": position, "waited_seconds": waited,
            "expired": bool(expired),
            "abandoned": abandoned,
            "meta": dict(e.meta or {}),
            "joined_at": e.joined_at.isoformat() if e.joined_at else None,
            "left_at": e.left_at.isoformat() if e.left_at else None}


async def queue_out(db: AsyncSession, row: ChannelQueue, *,
                    include_entries: bool = True) -> dict:
    cfg = queue_config(row.config)
    entries = await _entries(db, row.id)
    session_ids = [e.session_id for e in entries if e.session_id]
    sessions: dict[str, VoiceSession] = {}
    if session_ids:
        q = select(VoiceSession).where(VoiceSession.id.in_(session_ids))
        sessions = {s.id: s for s in (await db.execute(q)).scalars().all()}
    meeting_name = None
    if row.meeting_id:
        meeting = await db.get(VoiceMeeting, row.meeting_id)
        meeting_name = meeting.title if meeting is not None else None
    waiting = [e for e in entries if e.status == "waiting"]
    live_waiting = [e for e in waiting
                    if not _entry_out(e, sessions.get(e.session_id or ""),
                                      position=None)["abandoned"]]
    positions = {e.id: i + 1 for i, e in enumerate(live_waiting)}
    waited_of: dict[str, float] = {}
    for e in live_waiting:
        w = _entry_out(e, sessions.get(e.session_id or ""), position=positions[e.id],
                       max_wait_seconds=cfg["max_wait_seconds"])
        waited_of[e.id] = w["waited_seconds"] or 0.0
    longest = max(waited_of.values()) if waited_of else None
    out = {
        "id": row.id, "name": row.name, "state": row.state,
        "agent_id": row.agent_id or None,
        "meeting_id": row.meeting_id or None, "meeting_name": meeting_name,
        "config": cfg,
        "depth": {
            "waiting": len(live_waiting),
            "seated": sum(1 for e in entries if e.status == "seated"),
            "left": sum(1 for e in entries if e.status == "left"),
            "abandoned": sum(1 for e in waiting
                             if _entry_out(e, sessions.get(e.session_id or ""),
                                           position=None)["abandoned"]),
            "expired": sum(1 for w in waited_of.values() if w >= cfg["max_wait_seconds"]),
            "longest_wait_seconds": longest,
        },
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "notes": [
            "waiting = the call is HELD (session state on_hold) - py8n queues "
            "conversations, the provider's media plane does the holding sound",
            "positions and wait times are derived from joined_at order at read "
            "time, never stored; a caller whose session ended is derived abandoned",
            "seating (POST /next) releases the head from hold and attaches it to "
            "the destination meeting when one is bound (the SAME live call, now "
            "a meeting leg)",
        ],
    }
    if include_entries:
        shown = [e for e in entries if e.status in ("waiting", "seated")][-100:]
        out["entries"] = [_entry_out(e, sessions.get(e.session_id or ""),
                                     position=positions.get(e.id),
                                     max_wait_seconds=cfg["max_wait_seconds"])
                          for e in shown]
    return out


async def create_queue(db: AsyncSession, *, owner_id: str | None, name: str,
                       meeting_id: str | None = None, agent_id: str | None = None,
                       config: dict | None = None) -> dict:
    name = (name or "").strip()[:140]
    if not name:
        raise VoiceQueueError("a queue name is required")
    if meeting_id:
        meeting = await db.get(VoiceMeeting, meeting_id)
        if meeting is None or (owner_id is not None and meeting.owner_id is not None
                               and meeting.owner_id != owner_id):
            raise VoiceQueueError(f"destination meeting {meeting_id!r} not found")
    if agent_id:
        from .voice_agents import VoiceAgentError, _load as _load_agent

        try:
            agent = await _load_agent(db, agent_id, owner_id)
        except VoiceAgentError as exc:
            raise VoiceQueueError(str(exc)) from exc
        agent_id = agent.id
    row = ChannelQueue(owner_id=owner_id, name=name,
                       meeting_id=meeting_id or None, agent_id=agent_id or None,
                       config=queue_config(config),
                       state="open")
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return await queue_out(db, row)


async def get_queue(db: AsyncSession, queue_id: str, owner_id: str | None) -> dict:
    row = await _load(db, queue_id, owner_id)
    return await queue_out(db, row)


async def list_queues(db: AsyncSession, owner_id: str | None,
                      limit: int = 50) -> list[dict]:
    q = select(ChannelQueue).order_by(ChannelQueue.created_at.desc()).limit(max(1, min(limit, 200)))
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    return [await queue_out(db, r, include_entries=False) for r in rows]


async def set_queue_state(db: AsyncSession, owner_id: str | None, queue_id: str,
                          state: str) -> dict:
    row = await _load(db, queue_id, owner_id)
    state = (state or "").strip().lower()
    if state not in ("open", "closed"):
        raise VoiceQueueError("queue state must be open|closed")
    row.state = state
    db.add(row)
    await db.flush()
    return await queue_out(db, row)


async def enqueue(db: AsyncSession, owner_id: str | None, queue_id: str,
                  session_id: str, *, label: str = "") -> dict:
    """Put a LIVE call in the waiting room: the session is held
    (in_progress -> on_hold) and an entry books its place in line."""
    row = await _load(db, queue_id, owner_id)
    if row.state != "open":
        raise VoiceQueueError(f"the queue is {row.state} - closed queues take nobody")
    session = await db.get(VoiceSession, session_id)
    if session is None or (row.owner_id is not None and session.owner_id is not None
                           and session.owner_id != row.owner_id):
        raise VoiceQueueError(f"voice session {session_id!r} not found")
    # a session already sitting in a queue is "already waiting" - check it
    # BEFORE the state check (its state is on_hold BECAUSE it is waiting)
    dup_q = (select(ChannelQueueEntry)
             .where(ChannelQueueEntry.session_id == session.id,
                    ChannelQueueEntry.status == "waiting")
             .order_by(ChannelQueueEntry.joined_at.desc()))
    if (await db.execute(dup_q)).scalars().first() is not None:
        raise VoiceQueueError("this call is already waiting in a queue")
    if session.state != "in_progress":
        raise VoiceQueueError(
            f"queueing needs an in_progress call (someone to hold), got {session.state!r}")
    # one line at a time: a meeting leg belongs to its room, not a queue
    from ..models import VoiceMeetingParticipant

    leg_q = (select(VoiceMeetingParticipant)
             .where(VoiceMeetingParticipant.session_id == session.id)
             .order_by(VoiceMeetingParticipant.created_at.desc()))
    if (await db.execute(leg_q)).scalars().first() is not None:
        raise VoiceQueueError("this session is already a meeting leg - rooms queue nobody")
    cfg = queue_config(row.config)
    entries = await _entries(db, row.id)
    waiting = [e for e in entries if e.status == "waiting"
               and not _entry_out(e, await db.get(VoiceSession, e.session_id)
                                  if e.session_id else None, position=None)["abandoned"]]
    if len(waiting) >= cfg["max_size"]:
        raise VoiceQueueError(
            f"the queue is full ({cfg['max_size']} waiting) - seat or release someone first")
    # hold the call FIRST (fail loud before the row exists)
    await voice_svc.apply_event(db, session, "hold",
                                {"reason": "queued", "queue_id": row.id,
                                 "queue_name": row.name})
    entry = ChannelQueueEntry(queue_id=row.id, owner_id=row.owner_id,
                              session_id=session.id,
                              label=(label or "").strip()[:140]
                                    or session.from_ref or session.to_ref or "caller",
                              address=(session.from_ref if session.direction == "inbound"
                                       else session.to_ref) or "",
                              status="waiting",
                              meta={"held_at": _now().isoformat()})
    db.add(entry)
    await db.flush()
    out = await queue_out(db, row)
    out["entry_id"] = entry.id
    out["note"] = ("the call is on hold and holds its place in line - seating "
                   "releases it (and attaches it to the destination meeting when "
                   "one is bound)")
    return out


async def seat_next(db: AsyncSession, owner_id: str | None, queue_id: str, *,
                    meeting_id: str | None = None) -> dict:
    """Seat the head of the line: release the call from hold and - when a
    destination meeting exists (the queue's or the request's) - attach the
    SAME live session to the room as a participant leg. Abandoned heads
    (caller hung up) are skipped honestly, not seated."""
    row = await _load(db, queue_id, owner_id)
    dest_meeting = meeting_id or row.meeting_id
    entries = await _entries(db, row.id)
    head = None
    head_session = None
    for e in entries:
        if e.status != "waiting" or not e.session_id:
            continue
        session = await db.get(VoiceSession, e.session_id)
        if session is None or session.state == "ended":
            continue  # abandoned while waiting - derived, skipped
        head = e
        head_session = session
        break
    if head is None:
        raise VoiceQueueError("nobody is waiting (or everyone still in line already hung up)")
    attached = None
    if dest_meeting:
        from . import voice_meetings as meetings_svc

        # attach FIRST: a failed attach (room ended, room full) must not
        # leave a released call floating - the caller keeps their place
        attach = await meetings_svc.attach_session(
            db, row.owner_id, dest_meeting, head_session.id, label=head.label)
        attached = {"meeting_id": attach["meeting_id"],
                    "participant_id": attach["participant"]["id"]}
    released = await voice_svc.apply_event(db, head_session, "unhold",
                                           {"reason": "queue_seat", "queue_id": row.id})
    seat_meta = {"seated_at": _now().isoformat(), "via": "queue_seat"}
    if attached:
        seat_meta["meeting_id"] = attached["meeting_id"]
        seat_meta["participant_id"] = attached["participant_id"]
    head.status = "seated"
    head.left_at = _now()
    head.meta = {**(head.meta or {}), **seat_meta}
    db.add(head)
    await db.flush()
    out = await queue_out(db, row)
    out["seated"] = _entry_out(head, head_session, position=None)
    out["seated"]["released_state"] = released["state"]
    out["attached"] = attached
    out["note"] = ("the head of the line was released from hold"
                   + (f" and attached to meeting {attached['meeting_id'][:8]}"
                      if attached else " - no destination meeting bound, the call is "
                      "back on the line for whoever takes it"))
    return out


async def leave_queue(db: AsyncSession, owner_id: str | None, queue_id: str,
                      entry_id: str) -> dict:
    """Take one caller out of the line (they gave up on hold music or an
    operator pulled them): the entry closes and the call is RELEASED from
    hold back to in_progress - the conversation continues, nobody is
    silently dropped."""
    row = await _load(db, queue_id, owner_id)
    entry = await db.get(ChannelQueueEntry, entry_id)
    if entry is None or entry.queue_id != row.id:
        raise VoiceQueueError(f"queue entry {entry_id!r} not found in queue {queue_id!r}")
    if entry.status != "waiting":
        raise VoiceQueueError(f"the entry is already {entry.status}")
    session = await db.get(VoiceSession, entry.session_id) if entry.session_id else None
    if session is not None and session.state == "on_hold":
        await voice_svc.apply_event(db, session, "unhold",
                                    {"reason": "queue_leave", "queue_id": row.id})
    elif session is not None and session.state == "ended":
        pass  # abandoned - the entry still closes honestly
    entry.status = "left"
    entry.left_at = _now()
    entry.meta = {**(entry.meta or {}), "left_at_reason": "queue_leave"}
    db.add(entry)
    await db.flush()
    out = await queue_out(db, row)
    out["left"] = entry.id
    return out
