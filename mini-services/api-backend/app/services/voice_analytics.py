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
