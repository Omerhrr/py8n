"""Multi-party voice meetings (v74) - the room that owns legs.

A phone MEETING is not one call with more callers bolted on; it is a room
with legs. py8n is deliberately NOT the audio mixer - mixing is the
provider's media plane (a conference bridge, a SIP fork, a room service).
What py8n owns as the system-under-the-integrations:

* the participant list - who is in the room, on which channel, in which
  state (a web participant joins by attaching their browser's media
  stream to the leg's session websocket; a phone participant is DIALED
  through a provider adapter with a client_state that binds the carrier's
  call back to the participant row);
* one persona - every leg binds the SAME VoiceAgent (greeting, speech
  config, knowledge, brain), so the meeting's agent hears and answers
  per leg with the full v69..v73 machinery unchanged (state machine,
  ASR engine, handler, analytics);
* the merged transcript - derived at read time from the legs' event
  timelines (asr.final = the participant spoke, tts.started = the agent
  spoke), each line attributed to its speaker and leg. Never stored;
  the timelines are the record.

Legs are real VoiceSessions and dials are real calls - this is traffic
state (the same deliberate exception to derived-never-stored as campaign
targets and the deployment-token hit rows).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import VoiceEvent, VoiceMeeting, VoiceMeetingParticipant, VoiceSession
from . import voice as voice_svc
from .interactions import _handler_name as _wf_name
from .voice_agents import VoiceAgentError, _dataset_name, _credential_name, _load as _load_agent


class VoiceMeetingError(ValueError):
    """Honest 4xx-grade meeting failures."""


PARTICIPANT_CHANNELS = ("web", "telnyx", "sip")
MAX_PARTICIPANTS = 12
# v75: the per-member mix - what the SYSTEM gates, not what a mixer blends.
# py8n is not the audio mixer (the provider's media plane blends the audio);
# what py8n owns is the conversation pipeline, so the mix gates are:
#   muted    - the room does not HEAR this member (no ASR final, no turn)
#   deafened - the agent does not SPEAK to this member (TTS withheld)
#   solo     - spotlight/whisper: only this member's audio triggers turns
FLOOR_MODES = ("auto", "directed")


def _now():
    return datetime.now(timezone.utc)


async def _load(db: AsyncSession, meeting_id: str, owner_id: str | None) -> VoiceMeeting:
    row = await db.get(VoiceMeeting, meeting_id)
    if row is None:
        raise VoiceMeetingError(f"meeting {meeting_id!r} not found")
    if owner_id is not None and row.owner_id is not None and row.owner_id != owner_id:
        raise VoiceMeetingError(f"meeting {meeting_id!r} not found")
    return row


async def _participants(db: AsyncSession, meeting_id: str) -> list[VoiceMeetingParticipant]:
    q = (select(VoiceMeetingParticipant)
         .where(VoiceMeetingParticipant.meeting_id == meeting_id)
         .order_by(VoiceMeetingParticipant.created_at.asc()))
    return list((await db.execute(q)).scalars().all())


def client_state_for(meeting_id: str, participant_id: str) -> str:
    """The opaque carrier payload binding a call back to this leg."""
    raw = json.dumps({"mtg": meeting_id, "prt": participant_id}).encode()
    return base64.b64encode(raw).decode("ascii")


def decode_client_state(client_state: str) -> dict:
    """Decode a py8n-encoded client_state (best-effort, never fatal)."""
    try:
        data = json.loads(base64.b64decode(client_state or "").decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - a foreign client_state is not ours
        return {}


def mix_of(p: VoiceMeetingParticipant) -> dict:
    """The member's mix controls (v75) - defaults when never touched."""
    raw = (p.meta or {}).get("mix") or {}
    return {"muted": bool(raw.get("muted")),
            "deafened": bool(raw.get("deafened")),
            "solo": bool(raw.get("solo"))}


def floor_state_of(row: VoiceMeeting,
                   legs: list[VoiceMeetingParticipant] | None = None) -> dict:
    """The room's floor state (v75) - the live control lives in
    meeting.context["floor"]; labels resolve against the legs."""
    raw = (row.context or {}).get("floor") or {}
    mode = raw.get("mode") if raw.get("mode") in FLOOR_MODES else "auto"
    holder: VoiceMeetingParticipant | None = None
    if mode == "directed" and raw.get("participant_id"):
        pid = str(raw["participant_id"])
        for p in (legs or []):
            if p.id == pid:
                holder = p
                break
    return {"mode": mode,
            "participant_id": raw.get("participant_id") if mode == "directed" else None,
            "label": (holder.label or holder.address or "participant") if holder else None,
            "since": raw.get("since") if mode == "directed" else None,
            "note": ("directed: only the floor holder's audio triggers agent turns - "
                     "everyone else is still transcribed (the room hears) but gated" if mode == "directed"
                     else "auto: every live leg's audio triggers agent turns (v74 behavior)")}


def participant_out(p: VoiceMeetingParticipant, session: VoiceSession | None) -> dict:
    return {
        "id": p.id, "label": p.label, "channel": p.channel, "address": p.address,
        "state": p.state, "session_id": p.session_id,
        "call_control_id": p.call_control_id or None,
        "session_state": session.state if session is not None else None,
        "media_stream": (f"ws://<host>/api/v1/voice/sessions/{p.session_id}/media?token=<jwt>"
                         if p.channel == "web" and p.session_id else None),
        "mix": mix_of(p),
        "last_error": p.last_error or None,
        "joined_at": p.created_at.isoformat() if p.created_at else None,
        "left_at": p.left_at.isoformat() if p.left_at else None,
    }


async def meeting_out(db: AsyncSession, row: VoiceMeeting, *,
                      include_transcript: bool = True) -> dict:
    """The meeting as the API returns it - participants, per-leg session
    state, the FLOOR state (v75) and the DERIVED merged transcript
    (nothing stored)."""
    legs = await _participants(db, row.id)
    session_ids = [p.session_id for p in legs if p.session_id]
    sessions: dict[str, VoiceSession] = {}
    if session_ids:
        q = select(VoiceSession).where(VoiceSession.id.in_(session_ids))
        sessions = {s.id: s for s in (await db.execute(q)).scalars().all()}
    agent_name = None
    if row.agent_id:
        from ..models import VoiceAgent

        agent = await db.get(VoiceAgent, row.agent_id)
        agent_name = agent.name if agent is not None else None

    floor = floor_state_of(row, legs)

    out = {
        "id": row.id,
        "title": row.title,
        "state": row.state,
        "agent_id": row.agent_id,
        "agent_name": agent_name,
        "floor": floor,
        "participants": [participant_out(p, sessions.get(p.session_id or ""))
                         for p in legs],
        "counts": {
            "participants": len(legs),
            "joined": sum(1 for p in legs if p.state == "joined"),
            "live_legs": sum(1 for s in sessions.values() if s.state not in ("ended",)),
        },
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "notes": [
            "py8n is not the audio mixer: legs carry their own provider media "
            "(web legs attach to their session's media websocket, phone legs ride "
            "the provider's conference/bridge)",
            "v75 mix controls (muted/deafened/solo per member) and floor control "
            "(auto|directed) are SYSTEM gates enforced at the conversation pipeline: "
            "they shape what the agent hears and says per leg, not the provider's blend",
            "the transcript below is DERIVED from the legs' event timelines at read "
            "time - nothing transcript-shaped is stored",
        ],
    }
    if include_transcript:
        out["transcript"] = await merged_transcript(db, legs, sessions)
    return out


async def merged_transcript(db: AsyncSession, legs: list[VoiceMeetingParticipant],
                            sessions: dict[str, VoiceSession]) -> list[dict]:
    """The room's transcript: every leg's asr.final (the participant said)
    and tts.started (the agent said) merged in timeline order, attributed
    to the speaker. Derived, never stored."""
    session_ids = [p.session_id for p in legs if p.session_id]
    if not session_ids:
        return []
    label_by_session = {p.session_id: (p.label or p.address or "participant")
                        for p in legs if p.session_id}
    q = (select(VoiceEvent)
         .where(VoiceEvent.session_id.in_(session_ids))
         .order_by(VoiceEvent.created_at.asc(), VoiceEvent.id.asc()))
    events = (await db.execute(q)).scalars().all()
    lines: list[dict] = []
    for ev in events:
        payload = ev.payload or {}
        if ev.kind == "asr.final":
            lines.append({
                "at": ev.created_at.isoformat() if ev.created_at else None,
                "side": "participant",
                "speaker": label_by_session.get(ev.session_id, "participant"),
                "text": str(payload.get("transcript") or ""),
                "confidence": payload.get("confidence"),
            })
        elif ev.kind == "tts.started":
            text = str(payload.get("text") or "")
            if not text:
                continue
            lines.append({
                "at": ev.created_at.isoformat() if ev.created_at else None,
                "side": "agent",
                "speaker": "agent",
                "text": text,
                "source": payload.get("source") or "turn",
            })
    return lines


async def set_participant_mix(db: AsyncSession, owner_id: str | None,
                              meeting_id: str, participant_id: str, *,
                              muted: bool | None = None,
                              deafened: bool | None = None,
                              solo: bool | None = None) -> dict:
    """Set ONE member's mix controls (v75).

    Tri-state: omitted keys are left untouched. ``solo`` is exclusive -
    spotlighting one member clears every other member's solo flag. The
    controls are SYSTEM-level gates enforced where py8n owns the
    conversation pipeline (voice_turn); for provider legs the provider's
    media plane still blends the audio - py8n gates what the AGENT hears
    and says per leg, and says so."""
    meeting = await _load(db, meeting_id, owner_id)
    p = await db.get(VoiceMeetingParticipant, participant_id)
    if p is None or p.meeting_id != meeting.id:
        raise VoiceMeetingError(f"participant {participant_id!r} not found in meeting {meeting_id!r}")
    if meeting.state != "active":
        raise VoiceMeetingError("the meeting already ended - mix controls apply to active rooms")
    mix = dict((p.meta or {}).get("mix") or {})
    changed = {}
    for key, val in (("muted", muted), ("deafened", deafened), ("solo", solo)):
        if val is None:
            continue
        mix[key] = bool(val)
        changed[key] = bool(val)
    if not changed:
        raise VoiceMeetingError("nothing to set - pass muted / deafened / solo")
    if mix.get("solo"):
        # solo is exclusive: one spotlight at a time
        others = [o for o in await _participants(db, meeting_id) if o.id != p.id]
        for o in others:
            omix = dict((o.meta or {}).get("mix") or {})
            if omix.get("solo"):
                omix["solo"] = False
                o.meta = {**(o.meta or {}), "mix": omix}
                db.add(o)
    p.meta = {**(p.meta or {}), "mix": mix}
    db.add(p)
    await db.flush()
    return {"participant": participant_out(p, None),
            "changed": changed,
            "note": ("mix gates are enforced at the conversation pipeline: muted = the agent "
                     "does not hear this member, deafened = the agent does not speak to them, "
                     "solo = only their audio triggers turns")}


async def set_floor(db: AsyncSession, owner_id: str | None, meeting_id: str, *,
                    mode: str = "directed", participant_id: str | None = None) -> dict:
    """Floor control (v75): who the room's agent is talking to.

    * ``directed`` - the named participant holds the floor; every other
      member is still transcribed (the room hears) but their audio no
      longer triggers agent turns.
    * ``auto`` - the floor opens: every live leg's audio triggers turns
      again (the v74 behavior).
    """
    meeting = await _load(db, meeting_id, owner_id)
    if meeting.state != "active":
        raise VoiceMeetingError("the meeting already ended - floor control applies to active rooms")
    mode = (mode or "").strip().lower()
    if mode not in FLOOR_MODES:
        raise VoiceMeetingError(f"floor mode must be {'|'.join(FLOOR_MODES)}, got {mode!r}")
    ctx = dict(meeting.context or {})
    if mode == "auto":
        ctx["floor"] = {"mode": "auto"}
    else:
        if not participant_id:
            raise VoiceMeetingError(
                "directed floor needs participant_id (who holds the floor); "
                "mode=auto releases it")
        p = await db.get(VoiceMeetingParticipant, participant_id)
        if p is None or p.meeting_id != meeting.id:
            raise VoiceMeetingError(
                f"participant {participant_id!r} not found in meeting {meeting_id!r}")
        if p.state != "joined":
            raise VoiceMeetingError(
                f"the floor holder must be a joined leg, got {p.state!r}")
        ctx["floor"] = {"mode": "directed", "participant_id": p.id,
                        "since": _now().isoformat()}
    meeting.context = ctx
    db.add(meeting)
    await db.flush()
    return {"meeting": await meeting_out(db, meeting)}


async def meeting_gate_for_session(db: AsyncSession, session_id: str) -> dict | None:
    """The mix/floor gate that applies to THIS session's turns (v75).

    Resolved at turn time from the participant rows + the meeting's floor
    context. Returns None when the session is not a meeting leg or no
    gate applies. ``reason`` names WHY a turn would be gated - precedence
    muted > solo > floor (first match wins):
    * ``muted`` - this member is muted: the room does not hear them
    * ``solo``  - another member holds the solo spotlight
    * ``floor`` - the room is directed and another member holds the floor
    ``deafened`` rides along - it gates the agent's OUTPUT to this leg,
    not the input.
    """
    q = (select(VoiceMeetingParticipant)
         .where(VoiceMeetingParticipant.session_id == session_id)
         .order_by(VoiceMeetingParticipant.created_at.desc()))
    p = (await db.execute(q)).scalars().first()
    if p is None:
        return None
    meeting = await db.get(VoiceMeeting, p.meeting_id)
    if meeting is None:
        return None
    legs = await _participants(db, meeting.id)
    mix = mix_of(p)
    reason = None
    if mix["muted"]:
        reason = "muted"
    else:
        solo = next((o for o in legs if o.id != p.id and mix_of(o)["solo"]), None)
        if solo is not None:
            reason = "solo"
        else:
            floor = floor_state_of(meeting, legs)
            if (floor["mode"] == "directed" and floor["participant_id"]
                    and floor["participant_id"] != p.id):
                reason = "floor"
    return {"meeting_id": meeting.id, "participant_id": p.id,
            "label": p.label, "mix": mix, "deafened": mix["deafened"],
            "reason": reason,
            "floor": floor_state_of(meeting, legs)}


async def create_meeting(db: AsyncSession, *, owner_id: str | None,
                         agent_id: str | None, title: str = "") -> dict:
    title = (title or "").strip()[:200]
    if agent_id:
        await _load_agent(db, agent_id, owner_id)  # owner-scoped; raises VoiceAgentError
    row = VoiceMeeting(owner_id=owner_id, agent_id=agent_id or None,
                       title=title, context={})
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return await meeting_out(db, row)


async def get_meeting(db: AsyncSession, meeting_id: str, owner_id: str | None) -> dict:
    row = await _load(db, meeting_id, owner_id)
    return await meeting_out(db, row)


async def list_meetings(db: AsyncSession, owner_id: str | None,
                        limit: int = 50) -> list[dict]:
    q = select(VoiceMeeting).order_by(VoiceMeeting.created_at.desc()).limit(max(1, min(limit, 200)))
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    out = []
    for r in rows:
        d = await meeting_out(db, r, include_transcript=False)
        out.append(d)
    return out


async def _dial_participant(db: AsyncSession, owner_id: str | None,
                            meeting: VoiceMeeting, p: VoiceMeetingParticipant,
                            endpoint_id: str, *, sender=None) -> dict:
    """Dial a phone leg through a telnyx receiver (v74).

    ``sender(config, request) -> {status_code, json?}`` is injectable; the
    default performs the real HTTPS call. Without credentials the dial is
    built and honestly skipped - py8n never pretends it called someone."""
    from ..models import ChannelEndpoint
    from .channel_adapters import telnyx_build_dial
    from . import channel_adapters as adapters

    row = await db.get(ChannelEndpoint, endpoint_id)
    if row is None or (owner_id is not None and row.owner_id is not None
                       and row.owner_id != owner_id):
        raise VoiceMeetingError(f"channel endpoint {endpoint_id!r} not found")
    if (row.provider or "") not in ("telnyx", "telnyx_call_control", "telnyx_sms"):
        raise VoiceMeetingError(
            f"endpoint {row.name!r} is a {row.provider!r} receiver - meeting dials "
            "need a telnyx voice endpoint (api_key + connection_id)")
    config = row.config or {}
    base = str(config.get("base_url") or "").strip()
    webhook_url = str(config.get("webhook_url") or "").strip()
    connection_id = str(config.get("connection_id") or "").strip()
    if not connection_id:
        raise VoiceMeetingError(
            "the endpoint config carries no connection_id - set it (the Telnyx Call "
            "Control application id) so py8n can originate calls")
    if not webhook_url:
        raise VoiceMeetingError(
            "the endpoint config carries no webhook_url - set the absolute URL the "
            "provider posts call events to (your /api/v1/channels/telnyx/<id>/webhook)")
    request = telnyx_build_dial(
        config, to=p.address, from_ref=str(config.get("from_number") or p.address),
        connection_id=connection_id, webhook_url=webhook_url,
        client_state=client_state_for(meeting.id, p.id))
    masked = {"method": request["method"], "url": request["url"],
              "json": request.get("json")}
    if not str(config.get("api_key") or "").strip():
        return {"delivery": "skipped",
                "detail": "no api_key configured - the dial was built but NOT sent",
                "request": masked}
    if sender is None:
        async def _default_sender(cfg, req):
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(req["url"], json=req.get("json"),
                                         headers=req.get("headers", {}))
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                return {"status_code": resp.status_code, "json": body}
        sender = _default_sender
    try:
        result = await sender(config, request)
    except Exception as exc:  # noqa: BLE001 - the sandbox may have no egress
        return {"delivery": "failed",
                "detail": f"dial request failed: {exc}", "request": masked}
    ok = 200 <= int(result.get("status_code") or 0) < 300
    body = result.get("json") or {}
    return {"delivery": "delivered" if ok else "failed",
            "detail": f"telnyx answered {result.get('status_code')}",
            "call_control_id": str(((body.get("data") or {}).get("call_control_id")) or ""),
            "request": masked}


async def join_participant(db: AsyncSession, owner_id: str | None, meeting_id: str, *,
                           label: str, channel: str = "web", address: str = "",
                           endpoint_id: str | None = None, sender=None) -> dict:
    """Add a leg to the meeting.

    * ``web``     - a REAL VoiceSession is created (bound to the meeting's
                    agent); the browser attaches its media stream to the
                    session's websocket. Always works offline.
    * ``telnyx`` / ``sip`` - py8n DIALS the participant through a telnyx
                    receiver endpoint; the carrier's events (bound by
                    client_state) then create the leg's session through
                    the normal webhook path. Without credentials the dial
                    is honestly skipped.
    """
    meeting = await _load(db, meeting_id, owner_id)
    if meeting.state != "active":
        raise VoiceMeetingError("the meeting already ended - participants join active rooms only")
    channel = (channel or "web").strip().lower()
    if channel not in PARTICIPANT_CHANNELS:
        raise VoiceMeetingError(
            f"channel must be {'|'.join(PARTICIPANT_CHANNELS)}, got {channel!r}")
    label = (label or "").strip()[:140]
    if not label:
        raise VoiceMeetingError("a participant label is required")
    legs = await _participants(db, meeting_id)
    if len(legs) >= MAX_PARTICIPANTS:
        raise VoiceMeetingError(f"the meeting is full ({MAX_PARTICIPANTS} legs)")
    if channel in ("telnyx", "sip") and not (address or "").strip():
        raise VoiceMeetingError(f"a {channel} participant needs an address (E.164 or sip: URI)")

    p = VoiceMeetingParticipant(meeting_id=meeting_id, owner_id=owner_id,
                                label=label, channel=channel,
                                address=(address or "").strip()[:180], meta={})
    db.add(p)
    await db.flush()

    if channel == "web":
        session = await voice_svc.create_session(
            db, owner_id=owner_id, direction="inbound", provider="meeting",
            call_ref=f"meeting:{meeting_id}:{p.id}",
            from_ref=label, to_ref=f"meeting:{meeting.id}",
            agent_id=meeting.agent_id)
        await db.flush()
        row = await db.get(VoiceSession, session["id"])
        # the web leg arrives answered: walk the state machine to in_progress
        # so turns work immediately, then record the greeting via on_answered
        await voice_svc.apply_event(db, row, "call.ringing", {"source": "meeting_join"})
        await voice_svc.apply_event(db, row, "call.answered", {"source": "meeting_join"})
        from .voice_agents import on_answered as _on_agent_answered
        await _on_agent_answered(db, row)
        p.session_id = session["id"]
        p.state = "joined"
        db.add(p)
        await db.flush()
        return {"participant": participant_out(p, row),
                "meeting": await meeting_out(db, meeting)}

    # telnyx / sip - dial through the provider
    if not endpoint_id:
        p.state = "skipped"
        p.last_error = ("no endpoint_id given - pass a telnyx voice receiver "
                        "(api_key + connection_id) to dial phone legs")
        db.add(p)
        await db.flush()
        return {"participant": participant_out(p, None),
                "meeting": await meeting_out(db, meeting)}
    try:
        result = await _dial_participant(db, owner_id, meeting, p, endpoint_id,
                                         sender=sender)
    except VoiceMeetingError as exc:
        p.state = "failed"
        p.last_error = str(exc)[:400]
        db.add(p)
        await db.flush()
        return {"participant": participant_out(p, None),
                "meeting": await meeting_out(db, meeting)}
    delivery = result.get("delivery")
    if delivery == "delivered" and result.get("call_control_id"):
        p.state = "dialing"
        p.call_control_id = result["call_control_id"][:180]
    elif delivery == "delivered":
        p.state = "failed"
        p.last_error = "the dial was accepted but the response carried no call_control_id"
    else:
        p.state = "skipped" if delivery == "skipped" else "failed"
        p.last_error = str(result.get("detail") or "")[:400]
    p.meta = {"dial_request": result.get("request")}
    db.add(p)
    await db.flush()
    return {"participant": participant_out(p, None),
            "meeting": await meeting_out(db, meeting),
            "dial": {"delivery": delivery, "detail": result.get("detail"),
                     "request": result.get("request")}}


async def link_call(db: AsyncSession, *, call_control_id: str,
                    client_state: str = "", session=None) -> dict | None:
    """Bind a carrier call to its meeting leg (webhook side).

    Finds the participant by call_control_id (or decodes the call's
    client_state), then marks the leg joined and binds the meeting's agent
    to the session when the session has none. Best-effort: returns None
    when the call does not belong to any meeting leg."""
    p = None
    meeting = None
    if call_control_id:
        q = (select(VoiceMeetingParticipant)
             .where(VoiceMeetingParticipant.call_control_id == call_control_id)
             .order_by(VoiceMeetingParticipant.created_at.desc()))
        p = (await db.execute(q)).scalars().first()
    if p is None and client_state:
        state = decode_client_state(client_state)
        mtg, prt = state.get("mtg"), state.get("prt")
        if mtg and prt:
            meeting = await db.get(VoiceMeeting, str(mtg))
            if meeting is not None:
                p = await db.get(VoiceMeetingParticipant, str(prt))
                if p is not None and p.meeting_id != meeting.id:
                    p = None
    if p is None:
        return None
    meeting = meeting or await db.get(VoiceMeeting, p.meeting_id)
    if meeting is None:
        return None
    p.call_control_id = call_control_id or p.call_control_id
    if p.state in ("joining", "dialing"):
        p.state = "joined"
    bound = None
    if session is not None:
        p.session_id = session.id
        if meeting.agent_id and not ((session.context or {}).get("voice_agent")):
            try:
                from .voice_agents import bind_to_session

                await bind_to_session(db, session, meeting.agent_id, session.owner_id)
                bound = meeting.agent_id
            except VoiceAgentError:
                bound = None
    db.add(p)
    await db.flush()
    return {"meeting_id": meeting.id, "participant_id": p.id,
            "state": p.state, "agent_bound": bound}


async def end_meeting(db: AsyncSession, owner_id: str | None, meeting_id: str) -> dict:
    """End the room: hang up every live leg and mark the meeting ended."""
    row = await _load(db, meeting_id, owner_id)
    if row.state == "ended":
        raise VoiceMeetingError("the meeting already ended")
    legs = await _participants(db, meeting_id)
    hung_up = 0
    for p in legs:
        if p.session_id:
            session = await db.get(VoiceSession, p.session_id)
            if session is not None and session.state != "ended":
                await voice_svc.hangup(db, session, reason="meeting_ended")
                hung_up += 1
                p.state = "left"
                p.left_at = _now()
                db.add(p)
        elif p.state in ("joining", "dialing"):
            p.state = "left"
            p.left_at = _now()
            p.last_error = p.last_error or "meeting ended before the leg connected"
            db.add(p)
    row.state = "ended"
    row.ended_at = _now()
    db.add(row)
    await db.flush()
    return await meeting_out(db, row)
