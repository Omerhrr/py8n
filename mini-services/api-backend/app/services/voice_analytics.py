"""Voice session analytics (v73) - per-turn ASR confidence trends.

Every voice turn records an ``asr.final`` event whose payload carries the
transcript and the recognizer's confidence (v73: the vosk bridge reports
the MEAN of its per-word confidences, so a local engine produces REAL
confidence numbers instead of the 0.0 "not reported" default).

The analytics are DERIVED, never stored (the platform's oldest rule):
both endpoints replay the session's event timeline at read time.

* per-session  - the turn-by-turn confidence series, summary statistics,
  weak turns (below the 0.6 gate), and a least-squares TREND (improving /
  stable / degrading) over the turn indices - "is the caller being
  understood better or worse as the call goes on?"
* per-agent    - the same statistics pooled across the agent's sessions,
  plus a per-session breakdown - "is my phone agent's ASR healthy this
  week?"

Honesty: engines that cannot report confidence emit 0.0 (whisper.cpp
prints no confidence; a vosk build without word data does either). A
series of zeros is NOT a trend of catastrophic confidences - it is
reported as ``unknown`` with an exact note, and the affected turns are
counted as "unreported", never as weak.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import VoiceEvent, VoiceSession
from .voice_agents import VoiceAgentError, session_agent

from datetime import datetime, timezone

WEAK_CONFIDENCE = 0.6          # a reported turn below this is "weak"
TREND_EPSILON = 0.02           # |slope per turn| below this reads "stable"
MAX_AGENT_SESSIONS = 200       # agent analytics scan cap (honest, noted)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _trend(series: list[dict]) -> dict:
    """Least-squares slope of confidence over turn index (reported only)."""
    reported = [(int(s["turn_index"]), float(s["confidence"]))
                for s in series if float(s["confidence"]) > 0.0]
    unreported = len(series) - len(reported)
    if not reported:
        return {"direction": "unknown", "slope": None,
                "first_half_mean": None, "second_half_mean": None, "delta": None,
                "turns_measured": 0, "turns_unreported": unreported,
                "note": ("no turn in this window reported a confidence - the engine "
                         "does not emit one (whisper.cpp prints none; vosk without "
                         "word data does either); trend unavailable, never invented")}
    n = len(reported)
    mean_x = sum(x for x, _ in reported) / n
    mean_y = sum(y for _, y in reported) / n
    denom = sum((x - mean_x) ** 2 for x, _ in reported)
    slope = (sum((x - mean_x) * (y - mean_y) for x, y in reported) / denom
             if denom else 0.0)
    half = n // 2
    first = [y for _, y in reported[:half]] if half else [reported[0][1]]
    second = [y for _, y in reported[half:]] if n - half else [reported[-1][1]]
    fh, sh = _mean(first), _mean(second)
    if slope <= -TREND_EPSILON:
        direction = "degrading"
    elif slope >= TREND_EPSILON:
        direction = "improving"
    else:
        direction = "stable"
    return {"direction": direction, "slope": round(slope, 5),
            "first_half_mean": fh, "second_half_mean": sh,
            "delta": (round(sh - fh, 4) if fh is not None and sh is not None else None),
            "turns_measured": n, "turns_unreported": unreported,
            "note": "least squares over reported confidences only "
                    f"(|slope| <= {TREND_EPSILON} per turn reads stable)"}


def _series(events: list[VoiceEvent]) -> list[dict]:
    out = []
    for idx, ev in enumerate(events, start=1):
        payload = ev.payload or {}
        try:
            conf = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        out.append({
            "turn_index": idx, "event_id": ev.id,
            "confidence": round(max(0.0, min(1.0, conf)), 4),
            "transcript": str(payload.get("transcript") or "")[:160],
            "language": str(payload.get("language") or ""),
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
        })
    return out


def _summary(series: list[dict]) -> dict:
    reported = [float(s["confidence"]) for s in series if float(s["confidence"]) > 0.0]
    return {
        "turns": len(series),
        "turns_reported": len(reported),
        "turns_unreported": len(series) - len(reported),
        "mean": _mean(reported),
        "min": round(min(reported), 4) if reported else None,
        "max": round(max(reported), 4) if reported else None,
        "weak_turns": sum(1 for c in reported if c < WEAK_CONFIDENCE),
        "weak_gate": WEAK_CONFIDENCE,
    }


async def _asr_events(db: AsyncSession, session_id: str) -> list[VoiceEvent]:
    q = (select(VoiceEvent)
         .where(VoiceEvent.session_id == session_id, VoiceEvent.kind == "asr.final")
         .order_by(VoiceEvent.created_at.asc(), VoiceEvent.id.asc()))
    return list((await db.execute(q)).scalars().all())


async def session_analytics(db: AsyncSession, session: VoiceSession) -> dict:
    """Per-turn ASR confidence analytics for ONE call (derived, never stored)."""
    series = _series(await _asr_events(db, session.id))
    agent = session_agent(session) or {}
    weak = [{"turn_index": s["turn_index"], "confidence": s["confidence"],
             "transcript": s["transcript"]}
            for s in series if 0.0 < s["confidence"] < WEAK_CONFIDENCE]
    return {
        "session_id": session.id,
        "agent_id": agent.get("voice_agent_id"),
        "state": session.state,
        "provider": session.provider,
        "confidence": _summary(series),
        "series": series,
        "trend": _trend(series),
        "weak_turns": weak,
        "note": ("derived from the session's asr.final events - never stored; "
                 "confidence 0.0 means the engine did not report one, not a "
                 "catastrophic transcript"),
    }


async def agent_analytics(db: AsyncSession, agent_id: str, owner_id: str | None) -> dict:
    """ASR confidence analytics pooled across ONE agent's sessions."""
    from . import voice_agents as va_svc

    agent = await va_svc.get_agent(db, agent_id, owner_id)  # 404-grade on foreign/missing
    q = (select(VoiceSession).where(VoiceSession.state != "initiated")
         .order_by(VoiceSession.started_at.desc()).limit(MAX_AGENT_SESSIONS))
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    mine = [r for r in rows
            if ((r.context or {}).get("voice_agent") or {}).get("voice_agent_id") == agent_id]

    all_conf: list[float] = []
    per_session: list[dict] = []
    scanned = 0
    for sess in mine:
        events = await _asr_events(db, sess.id)
        if not events:
            continue
        scanned += 1
        series = _series(events)
        reported = [float(s["confidence"]) for s in series if float(s["confidence"]) > 0.0]
        all_conf.extend(reported)
        trend = _trend(series)
        per_session.append({
            "session_id": sess.id,
            "state": sess.state,
            "provider": sess.provider,
            "started_at": sess.started_at.isoformat() if sess.started_at else None,
            "confidence": _summary(series),
            "direction": trend["direction"],
            "slope": trend["slope"],
        })
    per_session.sort(key=lambda s: s["started_at"] or "", reverse=True)
    weak_total = sum(s["confidence"]["weak_turns"] for s in per_session)
    turns_total = sum(s["confidence"]["turns"] for s in per_session)
    knowledge = agent.get("knowledge") or {}
    return {
        "agent_id": agent_id,
        "agent_name": agent.get("name"),
        "sessions_scanned": scanned,
        "sessions_with_turns": scanned,
        "sessions_seen": len(mine),
        "sessions_cap": MAX_AGENT_SESSIONS,
        "sessions_skipped": max(0, len(mine) - MAX_AGENT_SESSIONS) or 0,
        "turns_total": turns_total,
        "confidence": {
            "mean": _mean(all_conf),
            "min": round(min(all_conf), 4) if all_conf else None,
            "max": round(max(all_conf), 4) if all_conf else None,
            "reported_turns": len(all_conf),
            "weak_turns": weak_total,
            "weak_turn_rate": (round(weak_total / len(all_conf), 4) if all_conf else None),
            "weak_gate": WEAK_CONFIDENCE,
        },
        "directions": {
            "improving": sum(1 for s in per_session if s["direction"] == "improving"),
            "stable": sum(1 for s in per_session if s["direction"] == "stable"),
            "degrading": sum(1 for s in per_session if s["direction"] == "degrading"),
            "unknown": sum(1 for s in per_session if s["direction"] == "unknown"),
        },
        "per_session": per_session[:50],
        "knowledge_bound": bool(knowledge.get("dataset_id")),
        "brain": (agent.get("brain") or {}).get("kind"),
        "note": ("pooled from each session's asr.final events - derived, never stored; "
                 "unreported confidences (0.0) are excluded from every statistic"),
    }


# ---------------------------------------------------------------------------
# v78: analytics on the NEW events - the line, the room's text side
# ---------------------------------------------------------------------------

MAX_QUEUE_ENTRIES_SCAN = 500   # per-queue entry scan cap (honest, noted)
MAX_MEETING_LEGS_SCAN = 100    # per-meeting leg scan cap (honest, noted)


def _p50(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 3)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 3)


def _delivery_split(events: list[VoiceEvent]) -> dict:
    """The delivered/failed/skipped split of queue.announced / queue.sms
    payloads - derived from the recorded delivery blocks, never re-run.
    announced records carry the delivery DICT, sms records the bare
    delivery string - both read honestly."""
    split: dict[str, int] = {}
    for ev in events:
        raw = (ev.payload or {}).get("delivery")
        if isinstance(raw, dict):
            d = str(raw.get("delivery") or "unknown")
        else:
            d = str(raw or "unknown")
        split[d] = split.get(d, 0) + 1
    return split


def _waits_of(entries: list, sessions: dict[str, VoiceSession]) -> dict:
    """Per-entry waits derived at read time: closed entries report
    left_at - joined_at (seat and leave both stamp left_at), still-waiting
    entries count to now; a caller whose session ended while waiting is
    derived abandoned (the v76 rule, kept)."""
    closed: list[float] = []
    seated: list[float] = []
    waiting_now: list[float] = []
    abandoned: list[float] = []
    now = datetime.now(timezone.utc)
    for e in entries:
        started = e.joined_at
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if started is None:
            continue
        if e.status in ("waiting",):
            session = sessions.get(e.session_id or "")
            if session is not None and session.state == "ended":
                abandoned.append((now - started).total_seconds())
            else:
                waiting_now.append((now - started).total_seconds())
        elif e.status in ("seated", "left", "callback"):
            ended = e.left_at or now
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=timezone.utc)
            w = max(0.0, (ended - started).total_seconds())
            closed.append(w)
            if e.status == "seated":
                seated.append(w)
    return {"closed": closed, "seated": seated,
            "waiting_now": waiting_now, "abandoned": abandoned}


def _wait_block(values: list[float]) -> dict:
    return {"count": len(values),
            "mean_seconds": (round(sum(values) / len(values), 3) if values else None),
            "p50_seconds": _p50(values),
            "max_seconds": (round(max(values), 3) if values else None)}


async def queue_analytics(db: AsyncSession, queue_id: str, owner_id: str | None) -> dict:
    """The line, measured (v78) - everything derived from the queue's own
    rows and its callers' event timelines:

    * WAITS - per-outcome wait distribution (seated / left / traded for a
      callback / abandoned-while-holding / still waiting now);
    * ABANDONMENT - the derived abandoned count and rate (the SLA's
      other face);
    * ANNOUNCEMENTS - how many times the queue spoke, and how the
      deliveries landed (web socket / provider speak / skipped / failed),
      derived from the queue.announced records;
    * SMS - the backchannel's delivery split, from the queue.sms records;
    * CALLBACKS - how many traded the hold for a callback and what the
      campaign did with them (entry statuses + the composed campaign's
      target counts).
    """
    from ..models import ChannelQueueEntry, VoiceSession as VS
    from .voice_queue import VoiceQueueError, _load as _load_queue, queue_config

    try:
        row = await _load_queue(db, queue_id, owner_id)
    except VoiceQueueError as exc:
        raise ValueError(str(exc)) from exc
    cfg = queue_config(row.config)
    q = (select(ChannelQueueEntry).where(ChannelQueueEntry.queue_id == row.id)
         .order_by(ChannelQueueEntry.joined_at.asc()).limit(MAX_QUEUE_ENTRIES_SCAN))
    entries = list((await db.execute(q)).scalars().all())
    session_ids = [e.session_id for e in entries if e.session_id]
    sessions: dict[str, VS] = {}
    if session_ids:
        sq = select(VS).where(VS.id.in_(session_ids))
        sessions = {s.id: s for s in (await db.execute(sq)).scalars().all()}
    waits = _waits_of(entries, sessions)
    status_counts: dict[str, int] = {}
    for e in entries:
        status_counts[e.status] = status_counts.get(e.status, 0) + 1
    ever_waiting = sum(1 for e in entries
                       if e.status in ("waiting", "seated", "left", "callback"))
    # the event timelines: one query over the callers' sessions
    announced_events: list[VoiceEvent] = []
    sms_events: list[VoiceEvent] = []
    if session_ids:
        eq = (select(VoiceEvent)
              .where(VoiceEvent.session_id.in_(session_ids),
                     VoiceEvent.kind.in_(("queue.announced", "queue.sms")))
              .order_by(VoiceEvent.created_at.asc()))
        for ev in (await db.execute(eq)).scalars().all():
            (announced_events if ev.kind == "queue.announced" else sms_events).append(ev)
    announcement_counts = [int((e.meta or {}).get("announcements") or 0) for e in entries]
    callback_entries = [e for e in entries if e.status == "callback"]
    callbacks = {"requested": len(callback_entries)}
    if callback_entries:
        from .voice_callbacks import callbacks_picture

        callbacks.update(await callbacks_picture(db, row))
    return {
        "queue_id": row.id, "name": row.name, "state": row.state,
        "config": cfg,
        "entries_scanned": len(entries),
        "entries_cap": MAX_QUEUE_ENTRIES_SCAN,
        "outcomes": status_counts,
        "waits": {
            "closed": _wait_block(waits["closed"]),
            "seated": _wait_block(waits["seated"]),
            "waiting_now": _wait_block(waits["waiting_now"]),
            "abandoned": _wait_block(waits["abandoned"]),
            "sla_breaches": sum(1 for w in (waits["closed"] + waits["abandoned"])
                                if w >= cfg["max_wait_seconds"]),
            "sla_seconds": cfg["max_wait_seconds"],
        },
        "abandonment": {
            "abandoned": len(waits["abandoned"]),
            "ever_waiting": ever_waiting,
            "rate": (round(len(waits["abandoned"]) / ever_waiting, 4)
                     if ever_waiting else None),
            "note": "abandoned = the caller hung up while holding (derived from the "
                    "session state at read time, never stored)",
        },
        "announcements": {
            "total": len(announced_events),
            "per_entry_max": (max(announcement_counts) if announcement_counts else 0),
            "delivery_split": _delivery_split(announced_events),
            "template": cfg["announce"]["template"] or "(default)",
        },
        "sms": {
            "total": len(sms_events),
            "delivery_split": _delivery_split(sms_events),
            "enabled": cfg["sms"]["enabled"],
        },
        "callbacks": callbacks,
        "note": ("derived from the queue's rows and the callers' event timelines "
                 "at read time - nothing analytical is stored; scanned up to "
                 f"{MAX_QUEUE_ENTRIES_SCAN} entries"),
    }


async def meeting_analytics(db: AsyncSession, meeting_id: str, owner_id: str | None) -> dict:
    """The room, measured (v78) - derived from the meeting's own rows:

    * LEGS - join state per channel (web / telnyx / sip);
    * CHAT - the text side channel by role (member / moderator / agent),
      the agent's replies and the ask-the-agent usage;
    * SPEAKING QUEUE - hands raised, currently waiting, and the
      raise -> outcome record (granted floor / lowered) with the WAIT
      between raising and the floor (kept in context when the entry
      leaves the queue - the pop destroys what a pure derivation needs);
    * TURNS + CONFIDENCE - per-leg transcript lines and the pooled
      per-turn ASR confidence summary (the v73 derivations, reused).
    """
    from ..models import VoiceMeetingMessage
    from .voice_meetings import (VoiceMeetingError, _load as _load_meeting,
                                 _participants as _meeting_participants)

    try:
        row = await _load_meeting(db, meeting_id, owner_id)
    except VoiceMeetingError as exc:
        raise ValueError(str(exc)) from exc
    legs = (await _meeting_participants(db, row.id))[:MAX_MEETING_LEGS_SCAN]
    session_ids = [p.session_id for p in legs if p.session_id]
    sessions: dict[str, VoiceSession] = {}
    if session_ids:
        sq = select(VoiceSession).where(VoiceSession.id.in_(session_ids))
        sessions = {s.id: s for s in (await db.execute(sq)).scalars().all()}
    channels: dict[str, int] = {}
    states: dict[str, int] = {}
    for p in legs:
        channels[p.channel] = channels.get(p.channel, 0) + 1
        states[p.state] = states.get(p.state, 0) + 1
    # chat: one query, grouped in python (the room is small)
    chat_roles: dict[str, int] = {}
    agent_replies = 0
    ask_agent = 0
    if row.id:
        cq = (select(VoiceMeetingMessage).where(VoiceMeetingMessage.meeting_id == row.id)
              .order_by(VoiceMeetingMessage.created_at.asc()))
        messages = list((await db.execute(cq)).scalars().all())
        for m in messages:
            chat_roles[m.role] = chat_roles.get(m.role, 0) + 1
            if m.role == "agent":
                agent_replies += 1
                # an agent reply IS an ask-the-agent usage (its meta carries
                # the member's question it answers)
                if (m.meta or {}).get("in_reply_to"):
                    ask_agent += 1
    else:
        messages = []
    # speaking queue: the live queue + the raise -> outcome history
    hand_raw = (row.context or {}).get("hand_queue") or {}
    hand_now = [e for e in (hand_raw.get("entries") or []) if isinstance(e, dict)]
    history = [e for e in ((row.context or {}).get("hand_history") or [])
               if isinstance(e, dict)]

    def _between(e: dict) -> float | None:
        try:
            raised = datetime.fromisoformat(str(e.get("raised_at")))
            resolved = datetime.fromisoformat(str(e.get("resolved_at")))
        except ValueError:
            return None
        if raised.tzinfo is None:
            raised = raised.replace(tzinfo=timezone.utc)
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=timezone.utc)
        return round(max(0.0, (resolved - raised).total_seconds()), 3)

    grants = [e for e in history if e.get("outcome") == "granted_floor"]
    grant_waits = [w for w in (_between(e) for e in grants) if w is not None]
    # turns + confidence per leg (the v73 series machinery, pooled)
    per_leg: list[dict] = []
    all_conf: list[float] = []
    participant_lines = agent_lines = 0
    for p in legs:
        if not p.session_id:
            continue
        evq = (select(VoiceEvent)
               .where(VoiceEvent.session_id == p.session_id,
                      VoiceEvent.kind.in_(("asr.final", "tts.started")))
               .order_by(VoiceEvent.created_at.asc(), VoiceEvent.id.asc()))
        events = list((await db.execute(evq)).scalars().all())
        said = [ev for ev in events if ev.kind == "asr.final"]
        spoken = [ev for ev in events if ev.kind == "tts.started"
                  and str((ev.payload or {}).get("text") or "").strip()]
        participant_lines += len(said)
        agent_lines += len(spoken)
        series = _series(said)
        reported = [float(s["confidence"]) for s in series if float(s["confidence"]) > 0.0]
        all_conf.extend(reported)
        per_leg.append({
            "participant_id": p.id, "label": p.label or p.address,
            "channel": p.channel, "state": p.state,
            "participant_lines": len(said),
            "agent_lines": len(spoken),
            "confidence": _summary(series),
            "direction": _trend(series)["direction"],
        })
    return {
        "meeting_id": row.id, "title": row.title, "state": row.state,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "legs_scanned": len(legs),
        "legs_cap": MAX_MEETING_LEGS_SCAN,
        "legs": {"by_channel": channels, "by_state": states,
                 "live": sum(1 for s in sessions.values() if s.state != "ended")},
        "chat": {"total": len(messages), "by_role": chat_roles,
                 "agent_replies": agent_replies, "ask_agent": ask_agent},
        "speaking_queue": {
            "waiting_now": len(hand_now),
            "resolved_total": len(history),
            "granted_floor": len(grants),
            "lowered": sum(1 for e in history if e.get("outcome") == "lowered"),
            "raise_to_floor_seconds": _wait_block(grant_waits),
            "note": ("raise -> floor waits are kept in the meeting's context when "
                     "the hand leaves the queue - the pop destroys the timestamp a "
                     "pure derivation would need"),
        },
        "conversation": {
            "participant_lines": participant_lines,
            "agent_lines": agent_lines,
            "confidence": {
                "mean": _mean(all_conf),
                "min": round(min(all_conf), 4) if all_conf else None,
                "max": round(max(all_conf), 4) if all_conf else None,
                "reported_turns": len(all_conf),
                "weak_turns": sum(1 for c in all_conf if c < WEAK_CONFIDENCE),
                "weak_gate": WEAK_CONFIDENCE,
            },
            "per_leg": per_leg[:50],
        },
        "note": ("derived from the meeting's rows, the room chat and the legs' "
                 "event timelines at read time - nothing analytical is stored"),
    }
