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

from ..models import VoiceEvent, VoiceMeeting, VoiceMeetingMessage, VoiceMeetingParticipant, VoiceSession
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
        "hand_queue": hand_queue_out(row, legs),
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
        # v76: the room's TEXT side channel size (the log itself rides
        # GET /meetings/{id}/chat - one query keeps the list view cheap)
        from sqlalchemy import func

        total = (await db.execute(
            select(func.count())
            .select_from(VoiceMeetingMessage)
            .where(VoiceMeetingMessage.meeting_id == row.id))).scalar()
        out["counts"]["chat_messages"] = int(total or 0)
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


# ---------------------------------------------------------------------------
# v76: attach a LIVE call as a leg (the queue's seat path)
# ---------------------------------------------------------------------------


async def attach_session(db: AsyncSession, owner_id: str | None, meeting_id: str,
                         session_id: str, *, label: str = "",
                         channel: str | None = None) -> dict:
    """Bind an EXISTING live call to the room as a participant leg (v76).

    This is how a caller walks from the channel queue's waiting room into
    the meeting WITHOUT a second call: the session stays the same
    VoiceSession, a participant row is created for it, and the room's
    agent binds when the session carries none yet. join_participant dials
    NEW legs; attach_session seats one that is already on the line.
    """
    meeting = await _load(db, meeting_id, owner_id)
    if meeting.state != "active":
        raise VoiceMeetingError("the meeting already ended - legs join active rooms only")
    session = await db.get(VoiceSession, session_id)
    if session is None or (owner_id is not None and session.owner_id is not None
                           and session.owner_id != owner_id):
        raise VoiceMeetingError(f"voice session {session_id!r} not found")
    if session.state == "ended":
        raise VoiceMeetingError("that call already ended - a room seats live calls only")
    dup = (select(VoiceMeetingParticipant)
           .where(VoiceMeetingParticipant.session_id == session.id,
                  VoiceMeetingParticipant.meeting_id == meeting.id)
           .order_by(VoiceMeetingParticipant.created_at.desc()))
    if (await db.execute(dup)).scalars().first() is not None:
        raise VoiceMeetingError("that call is already a leg of this meeting")
    legs = await _participants(db, meeting_id)
    if len(legs) >= MAX_PARTICIPANTS:
        raise VoiceMeetingError(f"the meeting is full ({MAX_PARTICIPANTS} legs)")
    derived_channel = session.provider if session.provider in ("web", "telnyx", "sip") else "web"
    use_channel = (channel or derived_channel).strip().lower()
    if use_channel not in PARTICIPANT_CHANNELS:
        use_channel = "web"
    p = VoiceMeetingParticipant(meeting_id=meeting_id, owner_id=owner_id,
                                label=(label or "").strip()[:140]
                                      or session.from_ref or session.to_ref or "caller",
                                channel=use_channel,
                                address=(session.from_ref if session.direction == "inbound"
                                         else session.to_ref) or "",
                                session_id=session.id, state="joined", meta={})
    db.add(p)
    await db.flush()
    bound = None
    if meeting.agent_id and not ((session.context or {}).get("voice_agent")):
        try:
            from .voice_agents import bind_to_session

            await bind_to_session(db, session, meeting.agent_id, session.owner_id)
            bound = meeting.agent_id
        except VoiceAgentError:
            bound = None
    await db.flush()
    return {"meeting_id": meeting.id,
            "participant": participant_out(p, session),
            "agent_bound": bound}


# ---------------------------------------------------------------------------
# v76: the room's group chat - the TEXT side channel
# ---------------------------------------------------------------------------


CHAT_ROLES = ("member", "moderator", "agent")


def _message_out(m: VoiceMeetingMessage) -> dict:
    return {"id": m.id, "meeting_id": m.meeting_id,
            "participant_id": m.participant_id or None,
            "session_id": m.session_id or None,
            "author": m.author, "role": m.role, "text": m.text,
            "meta": dict(m.meta or {}),
            "created_at": m.created_at.isoformat() if m.created_at else None}


async def post_chat_message(db: AsyncSession, owner_id: str | None, meeting_id: str, *,
                            participant_id: str | None = None, author: str = "",
                            text: str, ask_agent: bool = False) -> dict:
    """Post to the room's chat (v76).

    * with ``participant_id``  - a MEMBER speaks (the author is the leg's
      label; muted members type - chat is the one channel muting never
      gates, that is what it is FOR);
    * without                  - the MODERATOR speaks;
    * with ``ask_agent``       - the room's agent answers ON the asking
      member's leg: the question and the reply land on that leg's linked
      conversation (channel=meeting_chat) so the transcript stays ONE
      customer ONE context, and the reply is recorded in the room chat.
    """
    meeting = await _load(db, meeting_id, owner_id)
    if meeting.state != "active":
        raise VoiceMeetingError("the meeting already ended - chat belongs to active rooms")
    text = (text or "").strip()[:4000]
    if not text:
        raise VoiceMeetingError("a chat message needs text")

    participant: VoiceMeetingParticipant | None = None
    role = "moderator"
    author_name = (author or "").strip()[:140] or "moderator"
    if participant_id:
        participant = await db.get(VoiceMeetingParticipant, participant_id)
        if participant is None or participant.meeting_id != meeting.id:
            raise VoiceMeetingError(
                f"participant {participant_id!r} not found in meeting {meeting_id!r}")
        role = "member"
        author_name = participant.label or participant.address or "member"

    row = VoiceMeetingMessage(meeting_id=meeting.id, owner_id=owner_id,
                              participant_id=participant.id if participant else None,
                              session_id=participant.session_id if participant else None,
                              author=author_name, role=role, text=text, meta={})
    db.add(row)
    await db.flush()

    agent_reply: VoiceMeetingMessage | None = None
    if ask_agent:
        if not meeting.agent_id:
            raise VoiceMeetingError(
                "the room has no agent to ask - bind agent_id to the meeting first")
        if participant is None or not participant.session_id:
            raise VoiceMeetingError(
                "ask_agent needs a MEMBER's live leg to run the room agent over - "
                "moderator posts have no conversation to answer on")
        session = await db.get(VoiceSession, participant.session_id)
        if session is None or session.state == "ended":
            raise VoiceMeetingError("the asking member's call is not live anymore")
        if not session.conversation_id:
            raise VoiceMeetingError("the asking member's session has no linked conversation")
        # the SAME knowledge grounding voice_turn runs (the binding rides
        # the session's copied agent config)
        knowledge: list[dict] = []
        knowledge_note = ""
        kb = ((session.context or {}).get("voice_agent") or {}).get("knowledge") or {}
        if kb.get("dataset_id"):
            from . import knowledge as knowledge_svc

            try:
                kb_res = await knowledge_svc.knowledge_search(
                    db, dataset_id=kb["dataset_id"], query=text,
                    text_column=kb.get("text_column") or "",
                    answer_column=kb.get("answer_column") or None,
                    top_k=int(kb.get("top_k") or 1), owner_id=session.owner_id)
                knowledge = kb_res["matches"]
            except knowledge_svc.KnowledgeError as exc:
                knowledge_note = str(exc)
        from ..models import InteractionMessage

        db.add(InteractionMessage(conversation_id=session.conversation_id, role="user",
                                  channel="meeting_chat", text=text[:20000],
                                  payload={"meeting_id": meeting.id,
                                           "participant_id": participant.id,
                                           "chat_message_id": row.id}))
        await db.flush()
        # COMMIT before the handler runs - execute_workflow writes on its
        # own sessions (the SQLite single-writer discipline voice_turn
        # learned in v75)
        await db.commit()
        reply = await voice_svc._run_handler(db, session, text,
                                             knowledge=knowledge,
                                             knowledge_note=knowledge_note)
        if reply:
            db.add(InteractionMessage(conversation_id=session.conversation_id, role="agent",
                                      channel="meeting_chat", text=reply[:20000],
                                      payload={"via": "meeting_chat",
                                               "chat_message_id": row.id}))
            agent_reply = VoiceMeetingMessage(
                meeting_id=meeting.id, owner_id=owner_id, participant_id=None,
                session_id=participant.session_id, author="agent", role="agent",
                text=reply[:4000], meta={"in_reply_to": row.id,
                                         "asked_by": participant.id})
            db.add(agent_reply)
            await db.flush()
    out = {"message": _message_out(row), "meeting": await meeting_out(db, meeting)}
    if agent_reply is not None:
        out["agent_reply"] = _message_out(agent_reply)
    return out


async def get_chat(db: AsyncSession, meeting_id: str, owner_id: str | None,
                   limit: int = 100) -> list[dict]:
    """The room's chat log, chronological (the last ``limit`` messages)."""
    await _load(db, meeting_id, owner_id)
    q = (select(VoiceMeetingMessage)
         .where(VoiceMeetingMessage.meeting_id == meeting_id)
         .order_by(VoiceMeetingMessage.created_at.desc(), VoiceMeetingMessage.id.desc())
         .limit(max(1, min(limit, 500))))
    rows = list((await db.execute(q)).scalars().all())
    return [_message_out(m) for m in reversed(rows)]


# ---------------------------------------------------------------------------
# v76: the moderator's hand queue - speaking order as room state
# ---------------------------------------------------------------------------


def _hand_entries(meeting: VoiceMeeting) -> list[dict]:
    raw = (meeting.context or {}).get("hand_queue") or {}
    entries = raw.get("entries") or []
    return [e for e in entries if isinstance(e, dict) and e.get("participant_id")]


def hand_queue_out(meeting: VoiceMeeting,
                   legs: list[VoiceMeetingParticipant]) -> dict:
    """The speaking queue with derived positions + wait times (nothing
    but the raised hands themselves is stored - in meeting.context, the
    same place the floor lives)."""
    label_of = {p.id: (p.label or p.address or "participant") for p in legs}
    now = _now()
    entries = []
    for i, e in enumerate(_hand_entries(meeting)):
        raised = None
        try:
            raised = datetime.fromisoformat(str(e.get("raised_at")))
        except ValueError:
            raised = None
        waited = round(max(0.0, (now - raised).total_seconds()), 3) if raised else None
        entries.append({"position": i + 1, "participant_id": e["participant_id"],
                        "label": label_of.get(e["participant_id"], "participant"),
                        "raised_at": e.get("raised_at"),
                        "waited_seconds": waited,
                        "note": str(e.get("note") or "")[:200]})
    return {"count": len(entries), "entries": entries}


async def raise_hand(db: AsyncSession, owner_id: str | None, meeting_id: str,
                     participant_id: str, *, note: str = "") -> dict:
    """A member asks for the floor (or the moderator queues them)."""
    meeting = await _load(db, meeting_id, owner_id)
    if meeting.state != "active":
        raise VoiceMeetingError("the meeting already ended - hands raise in active rooms")
    p = await db.get(VoiceMeetingParticipant, participant_id)
    if p is None or p.meeting_id != meeting.id:
        raise VoiceMeetingError(
            f"participant {participant_id!r} not found in meeting {meeting_id!r}")
    if p.state != "joined":
        raise VoiceMeetingError(f"only joined legs raise hands, got {p.state!r}")
    entries = _hand_entries(meeting)
    if any(e["participant_id"] == p.id for e in entries):
        raise VoiceMeetingError(
            f"{p.label or p.address!r} is already in the speaking queue")
    if len(entries) >= MAX_PARTICIPANTS:
        raise VoiceMeetingError(f"the speaking queue is full ({MAX_PARTICIPANTS})")
    ctx = dict(meeting.context or {})
    ctx["hand_queue"] = {"entries": entries + [{
        "participant_id": p.id, "raised_at": _now().isoformat(),
        "note": (note or "").strip()[:200]}]}
    meeting.context = ctx
    db.add(meeting)
    await db.flush()
    legs = await _participants(db, meeting_id)
    return {"hand_queue": hand_queue_out(meeting, legs),
            "participant": participant_out(p, None)}


async def lower_hand(db: AsyncSession, owner_id: str | None, meeting_id: str,
                     participant_id: str) -> dict:
    """Remove one raised hand (the member lowered it or the moderator
    declined the request)."""
    meeting = await _load(db, meeting_id, owner_id)
    entries = _hand_entries(meeting)
    remaining = [e for e in entries if e["participant_id"] != participant_id]
    if len(remaining) == len(entries):
        raise VoiceMeetingError(
            f"participant {participant_id!r} is not in the speaking queue")
    ctx = dict(meeting.context or {})
    ctx["hand_queue"] = {"entries": remaining}
    meeting.context = ctx
    db.add(meeting)
    await db.flush()
    legs = await _participants(db, meeting_id)
    return {"hand_queue": hand_queue_out(meeting, legs)}


async def call_next_hand(db: AsyncSession, owner_id: str | None,
                         meeting_id: str) -> dict:
    """Call the NEXT hand in line: the head is popped and GRANTED THE
    FLOOR (directed floor - the same primitive the moderator points with,
    now fed by the queue's order)."""
    meeting = await _load(db, meeting_id, owner_id)
    if meeting.state != "active":
        raise VoiceMeetingError("the meeting already ended - queues belong to active rooms")
    entries = _hand_entries(meeting)
    if not entries:
        raise VoiceMeetingError("nobody is waiting to speak")
    head, rest = entries[0], entries[1:]
    ctx = dict(meeting.context or {})
    ctx["hand_queue"] = {"entries": rest}
    meeting.context = ctx
    db.add(meeting)
    await db.flush()
    floor = await set_floor(db, owner_id, meeting_id, mode="directed",
                            participant_id=head["participant_id"])
    legs = await _participants(db, meeting_id)
    return {"called": head["participant_id"],
            "hand_queue": hand_queue_out(meeting, legs),
            "floor": (floor.get("meeting") or {}).get("floor"),
            "meeting": floor.get("meeting")}


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
