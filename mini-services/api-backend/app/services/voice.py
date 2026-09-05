"""Voice session primitives (v69) - the phone call as a first-class object.

The voice thesis from the architecture discussion: py8n is the SYSTEM
layer on top of telecom infrastructure, so it needs the call primitives
every voice agent is built from, provider-agnostic:

* **call state machine** - initiated -> ringing -> in_progress -> ended,
  with on_hold / voicemail side states and honest endings (no_answer,
  busy, failed). Illegal transitions are refused, not absorbed.
* **barge-in** - the caller starts speaking while the agent is still
  talking; the active TTS utterance is cancelled (tts.ended with
  cancelled=true), the barge_in event references what it interrupted,
  and the count is derived from the event timeline.
* **ASR/TTS contract** - the provider-agnostic request/response shapes
  every speech adapter maps to (whisper/deepgram/elevenlabs/piper...).
  A ``voice_turn`` runs the contract end to end: ASR result -> user
  message on the linked conversation -> handler workflow -> agent reply
  -> TTS request, exactly the interaction-layer handler convention.

The event timeline (VoiceEvent) is the record; duration, barge-in
count, turn count are all DERIVED from it at read time.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import VoiceEvent, VoiceSession, Workflow
from .interactions import _load_messages as _conv_messages


class VoiceError(ValueError):
    """Honest 4xx-grade voice failures."""


# ---------------------------------------------------------------------------
# The call state machine
# ---------------------------------------------------------------------------

STATES = ("initiated", "ringing", "in_progress", "on_hold", "voicemail", "ended")

# legal transitions - everything else is refused with the exact reason
_TRANSITIONS: dict[str, set[str]] = {
    "initiated": {"ringing", "in_progress", "ended"},
    "ringing": {"in_progress", "ended"},           # ended carries no_answer/busy/failed
    "in_progress": {"on_hold", "ended"},
    "on_hold": {"in_progress", "ended"},
    "voicemail": {"ended"},
    "ended": set(),
}

EVENT_KINDS = (
    "call.ringing", "call.answered", "speech.started", "speech.ended",
    "dtmf", "asr.final", "tts.started", "tts.ended", "barge_in",
    "hold", "unhold", "transfer", "no_answer", "busy",
    "voicemail_detected", "hangup", "failed",
    # v70: media transport (websocket audio streams) bookkeeping
    "media.stream_started", "media.stream_stopped",
)

END_KINDS = {  # event kind -> end_reason recorded on the session
    "no_answer": "no_answer",
    "busy": "busy",
    "failed": "failed",
    "hangup": "hangup",
}

ASR_PROVIDERS: dict[str, dict] = {
    "openai_whisper": {"request": "POST {base}/v1/audio/transcriptions (multipart: file, model, language)",
                       "map": {"text": "transcript", "language": "language"},
                       "confidence": "absent in the API - defaults recorded honestly"},
    "deepgram": {"request": "POST {base}/v1/listen?model=nova-2 (raw audio or websocket)",
                 "map": {"results.channels[0].alternatives[0].transcript": "transcript",
                          "results.channels[0].alternatives[0].confidence": "confidence"}},
    "assemblyai": {"request": "POST {base}/v2/transcript + polling (or realtime websocket)",
                   "map": {"text": "transcript", "confidence": "confidence"}},
    "py8n_local": {"request": "in-process whisper.cpp / vosk binding",
                   "map": {"text": "transcript", "confidence": "confidence"}},
}

TTS_PROVIDERS: dict[str, dict] = {
    "openai_tts": {"request": "POST {base}/v1/audio/speech {model, input, voice, response_format}",
                   "audio": "binary stream (mp3/wav/opus)"},
    "elevenlabs": {"request": "POST {base}/v1/text-to-speech/{voice_id} {text, model_id}",
                   "audio": "binary stream (mp3/wav)"},
    "piper_local": {"request": "in-process piper synthesis (voice on disk)",
                    "audio": "wav bytes"},
    "meta_mms": {"request": "POST {base} MMS-TTS checkpoint (transformers pipeline)",
                 "audio": "wav bytes"},
}

ASR_CONTRACT = {
    "input": {"audio": {"format": "wav|mp3|mulaw|opus", "sample_rate": 8000,
                        "encoding": "linear16|mulaw|flac"},
              "language": "optional bcp-47 hint, e.g. en-NG"},
    "output": {"transcript": "str (required)", "confidence": "float 0..1 (required)",
               "language": "str", "is_final": "bool", "duration_ms": "int"},
    "rule": "a partial (is_final=false) never triggers a handler turn",
}

TTS_CONTRACT = {
    "input": {"text": "str", "voice": "provider voice id", "format": "wav|mp3|opus",
              "barge_in_ok": "bool - may the caller interrupt this utterance"},
    "output": {"audio_ref": "registry/asset ref OR audio_b64", "format": "str",
               "duration_estimate_ms": "int"},
    "rule": "tts.started opens an interruptible utterance; barge-in cancels it",
}


def validate_asr_result(data: dict) -> dict:
    """Normalize an ASR result into the contract shape (fail loud)."""
    if not isinstance(data, dict):
        raise VoiceError("asr result must be an object")
    transcript = str(data.get("transcript") or "").strip()
    if not transcript:
        raise VoiceError("asr result requires a non-empty 'transcript'")
    confidence = data.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        raise VoiceError("asr 'confidence' must be a number") from None
    if not 0.0 <= confidence <= 1.0:
        raise VoiceError(f"asr 'confidence' must be within 0..1, got {confidence}")
    return {"transcript": transcript, "confidence": confidence,
            "language": str(data.get("language") or ""),
            "is_final": bool(data.get("is_final", True)),
            "duration_ms": int(data.get("duration_ms") or 0)}


def validate_tts_result(data: dict) -> dict:
    """Normalize a TTS result into the contract shape (fail loud)."""
    if not isinstance(data, dict):
        raise VoiceError("tts result must be an object")
    audio_ref = str(data.get("audio_ref") or "")
    audio_b64 = str(data.get("audio_b64") or "")
    if not audio_ref and not audio_b64:
        raise VoiceError("tts result requires 'audio_ref' or 'audio_b64'")
    fmt = str(data.get("format") or "wav")
    if fmt not in ("wav", "mp3", "opus", "mulaw"):
        raise VoiceError(f"tts 'format' must be wav|mp3|opus|mulaw, got {fmt!r}")
    return {"audio_ref": audio_ref, "audio_b64": audio_b64, "format": fmt,
            "duration_estimate_ms": int(data.get("duration_estimate_ms") or 0)}


def build_tts_request(text: str, provider: str = "openai_tts", voice: str = "alloy",
                      fmt: str = "wav", barge_in_ok: bool = True) -> dict:
    """The provider-bound TTS request the voice bridge would execute."""
    if provider not in TTS_PROVIDERS:
        raise VoiceError(f"unknown tts provider {provider!r} - known: "
                         f"{', '.join(sorted(TTS_PROVIDERS))}")
    if not str(text or "").strip():
        raise VoiceError("tts request requires text")
    if fmt not in ("wav", "mp3", "opus", "mulaw"):
        raise VoiceError(f"tts 'format' must be wav|mp3|opus|mulaw, got {fmt!r}")
    return {"provider": provider, "text": text, "voice": voice, "format": fmt,
            "barge_in_ok": bool(barge_in_ok),
            "contract": TTS_PROVIDERS[provider]["request"]}


def parse_twilio_status(payload: dict) -> tuple[str, str, dict]:
    """Translate a Twilio call-status callback into a session event.

    Returns (call_ref, event_kind, payload). CallStatus mapping:
    queued/ringing -> call.ringing, in-progress -> call.answered,
    completed -> hangup, busy -> busy, no-answer -> no_answer,
    canceled/failed -> failed. Pure - the API layer applies it.
    """
    call_ref = str(payload.get("CallSid") or payload.get("call_sid") or "")
    if not call_ref:
        raise VoiceError("twilio status callback requires CallSid")
    status = str(payload.get("CallStatus") or payload.get("call_status") or "").strip().lower()
    kind = {"queued": "call.ringing", "ringing": "call.ringing",
            "in-progress": "call.answered", "completed": "hangup",
            "busy": "busy", "no-answer": "no_answer",
            "canceled": "failed", "failed": "failed"}.get(status)
    if kind is None:
        raise VoiceError(f"unknown twilio CallStatus {status!r}")
    extra: dict = {}
    if payload.get("CallDuration") is not None:
        extra["provider_duration_seconds"] = payload.get("CallDuration")
    return call_ref, kind, extra


# ---------------------------------------------------------------------------
# Session service
# ---------------------------------------------------------------------------

def _event_out(row: VoiceEvent) -> dict:
    return {"id": row.id, "kind": row.kind, "payload": row.payload or {},
            "created_at": row.created_at.isoformat() if row.created_at else None}


def session_out(row: VoiceSession, events: list[VoiceEvent] | None = None,
                conversation_summary: dict | None = None) -> dict:
    evs = events or []
    duration = None
    if row.ended_at and row.started_at:
        duration = round((row.ended_at - row.started_at).total_seconds(), 3)
    elif events and row.state != "ended":
        duration = None  # still live - no made-up numbers
    return {
        "id": row.id,
        "direction": row.direction,
        "provider": row.provider,
        "call_ref": row.call_ref,
        "from": row.from_ref,
        "to": row.to_ref,
        "state": row.state,
        "end_reason": row.end_reason,
        "handler_workflow_id": row.handler_workflow_id,
        "conversation_id": row.conversation_id,
        "conversation": conversation_summary,
        # v71: the VoiceAgent config this session copied at creation
        # (config is copied, not referenced - editing the agent never
        # rewrites a live call's history)
        "agent": {k: v for k, v in ((row.context or {}).get("voice_agent") or {}).items()
                  if k != "system_prompt"} or None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "answered_at": row.answered_at.isoformat() if row.answered_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "duration_seconds": duration,
        # derived from the event timeline - never stored
        "barge_in_count": sum(1 for e in evs if e.kind == "barge_in"),
        "turn_count": sum(1 for e in evs if e.kind == "asr.final"),
        "active_tts": bool((row.context or {}).get("active_tts")),
        # v70: the media transport's honest counters (stream_sid/chunks/
        # audio_ms/skipped/opened/stopped) as the websocket left them
        "media": dict((row.context or {}).get("media") or {}) or None,
        "events": [_event_out(e) for e in evs] if events is not None else None,
        "event_count": len(evs),
    }


async def _load_events(db: AsyncSession, session_id: str) -> list[VoiceEvent]:
    q = (select(VoiceEvent).where(VoiceEvent.session_id == session_id)
         .order_by(VoiceEvent.created_at.asc(), VoiceEvent.id.asc()))
    return list((await db.execute(q)).scalars().all())


async def _conversation_summary(db: AsyncSession, conversation_id: str | None) -> dict | None:
    if not conversation_id:
        return None
    from .interactions import _extract_reply  # noqa: F401  (kept: reply keys shared)
    msgs = await _conv_messages(db, conversation_id)
    return {"id": conversation_id, "messages": len(msgs),
            "roles": sorted({m.role for m in msgs})}


async def get_session(db: AsyncSession, session_id: str, owner_id: str | None,
                      with_events: bool = True) -> dict | None:
    row = await db.get(VoiceSession, session_id)
    if row is None:
        return None
    if owner_id is not None and row.owner_id is not None and row.owner_id != owner_id:
        return None
    events = await _load_events(db, row.id) if with_events else []
    return session_out(row, events if with_events else None,
                       await _conversation_summary(db, row.conversation_id))


async def list_sessions(db: AsyncSession, owner_id: str | None,
                        state: str | None = None, limit: int = 100) -> list[dict]:
    q = select(VoiceSession).order_by(VoiceSession.started_at.desc())
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    if state:
        rows = [r for r in rows if r.state == state]
    out = []
    for r in rows[: max(1, min(limit, 200))]:
        out.append(await get_session(db, r.id, owner_id))
    return [s for s in out if s is not None]


async def _add_event(db: AsyncSession, session: VoiceSession, kind: str,
                     payload: dict | None = None) -> VoiceEvent:
    if kind not in EVENT_KINDS:
        raise VoiceError(f"unknown voice event kind {kind!r} - known: "
                         f"{', '.join(EVENT_KINDS)}")
    row = VoiceEvent(session_id=session.id, kind=kind, payload=payload or {})
    db.add(row)
    await db.flush()
    return row


async def create_session(db: AsyncSession, *, owner_id: str | None, direction: str = "inbound",
                         provider: str = "twilio", call_ref: str = "", from_ref: str = "",
                         to_ref: str = "", handler_workflow_id: str | None = None,
                         conversation_ref: str | None = None,
                         agent_id: str | None = None) -> dict:
    """Open a call. Links (or opens) the interaction-layer conversation.

    The conversation link is the point: the SAME customer talking on
    voice then whatsapp is ONE transcript. Outbound dials start in
    'initiated' and reach 'ringing' via events; inbound calls arrive
    already ringing. ``agent_id`` (v71) copies the VoiceAgent's config
    into the session context at creation and falls the handler back to
    the agent's workflow when none is given - the greeting, the ASR
    engine and the TTS voice of every turn then come from the agent.
    """
    direction = (direction or "inbound").strip().lower()
    if direction not in ("inbound", "outbound"):
        raise VoiceError("direction must be inbound|outbound")
    if handler_workflow_id:
        wf = await db.get(Workflow, handler_workflow_id)
        if wf is None or (owner_id is not None and wf.owner_id is not None
                          and wf.owner_id != owner_id):
            raise VoiceError(f"handler workflow {handler_workflow_id!r} not found")

    agent_handler_id: str | None = None
    from .interactions import create_conversation, get_conversation
    if conversation_ref:
        conv = await get_conversation(db, conversation_ref, owner_id)
        if conv is None:
            raise VoiceError(f"conversation_ref {conversation_ref!r} not found")
        conversation_id = conv["id"]
    else:
        conv = await create_conversation(
            db, owner_id=owner_id, channel="voice",
            participant_id=(from_ref if direction == "inbound" else to_ref)[:180],
            participant_name="",
            handler_workflow_id=handler_workflow_id,
            context={"voice": {"direction": direction, "provider": provider}})
        conversation_id = conv["id"]

    row = VoiceSession(
        direction=direction, provider=(provider or "twilio").strip()[:40],
        call_ref=(call_ref or "")[:180],
        from_ref=(from_ref or "")[:180], to_ref=(to_ref or "")[:180],
        handler_workflow_id=handler_workflow_id,
        conversation_id=conversation_id,
        state="initiated",
    )
    row.owner_id = owner_id
    db.add(row)
    await db.flush()

    if agent_id:
        from . import voice_agents
        try:
            agent_handler_id = await voice_agents.bind_to_session(db, row, agent_id, owner_id)
        except voice_agents.VoiceAgentError as exc:
            raise VoiceError(str(exc)) from exc
        if agent_handler_id and not row.handler_workflow_id:
            row.handler_workflow_id = agent_handler_id
            db.add(row)
            if agent_handler_id:
                # the conversation's handler rides the agent's workflow too
                from ..models import InteractionConversation
                conv_row = await db.get(InteractionConversation, conversation_id)
                if conv_row is not None and not conv_row.handler_workflow_id:
                    conv_row.handler_workflow_id = agent_handler_id
                    db.add(conv_row)
    await db.flush()
    await db.refresh(row)
    return await get_session(db, row.id, owner_id)  # type: ignore[return-value]


async def apply_event(db: AsyncSession, session: VoiceSession, kind: str,
                      payload: dict | None = None) -> dict:
    """Run one event through the state machine (and its side effects)."""
    if session.state == "ended":
        raise VoiceError(f"the call already ended (reason: {session.end_reason or 'hangup'}) - "
                         "open a new session for the next call")
    payload = payload or {}

    if kind == "call.ringing":
        if "ringing" not in _TRANSITIONS[session.state]:
            raise VoiceError(f"illegal transition {session.state} -> ringing")
        session.state = "ringing"
    elif kind in ("call.answered",):
        if "in_progress" not in _TRANSITIONS[session.state]:
            raise VoiceError(f"illegal transition {session.state} -> in_progress (call not ringing)")
        session.state = "in_progress"
        session.answered_at = datetime.now(timezone.utc)
    elif kind in ("speech.started", "speech.ended", "dtmf", "voicemail_detected"):
        if kind == "voicemail_detected" and session.state not in ("ringing", "in_progress"):
            raise VoiceError(f"voicemail detection needs an active or ringing call, got {session.state}")
        if kind == "voicemail_detected":
            session.state = "voicemail"
    elif kind == "hold":
        if "on_hold" not in _TRANSITIONS[session.state]:
            raise VoiceError(f"cannot hold a call in state {session.state}")
        session.state = "on_hold"
    elif kind == "unhold":
        if "in_progress" not in _TRANSITIONS[session.state]:
            raise VoiceError(f"cannot unhold a call in state {session.state}")
        session.state = "in_progress"
    elif kind == "transfer":
        if session.state != "in_progress":
            raise VoiceError(f"transfers need an in_progress call, got {session.state}")
    elif kind in END_KINDS:
        if "ended" not in _TRANSITIONS[session.state]:
            raise VoiceError(f"illegal transition {session.state} -> ended")
        session.state = "ended"
        session.end_reason = END_KINDS[kind]
        session.ended_at = datetime.now(timezone.utc)
    else:
        raise VoiceError(f"event {kind!r} is not applied by the state machine "
                         "(asr.final and barge-in have dedicated endpoints)")

    event = await _add_event(db, session, kind, payload)
    if kind == "hangup" and payload.get("reason"):
        session.end_reason = str(payload["reason"])[:40]
    db.add(session)
    await db.flush()
    return {"event": _event_out(event), "state": session.state,
            "end_reason": session.end_reason or None}


async def barge_in(db: AsyncSession, session: VoiceSession) -> dict:
    """The caller interrupts the agent mid-sentence.

    Cancels the active TTS utterance (tts.ended cancelled=true), records
    the barge_in event referencing what it interrupted, clears the
    active-utterance pointer. Nothing playing -> honest 400.
    """
    if session.state == "ended":
        raise VoiceError("the call already ended")
    active_id = (session.context or {}).get("active_tts")
    if not active_id:
        raise VoiceError("nothing is playing - barge-in needs an active TTS utterance "
                         "(start one with a voice turn or tts.started)")
    ctx = dict(session.context or {})
    ctx.pop("active_tts", None)
    session.context = ctx
    db.add(session)
    await _add_event(db, session, "barge_in", {"interrupted": active_id})
    await _add_event(db, session, "tts.ended", {"tts_id": active_id, "cancelled": True})
    await db.flush()
    return {"state": session.state, "interrupted": active_id,
            "barge_in_count": sum(1 for e in (await _load_events(db, session.id))
                                  if e.kind == "barge_in")}


async def _run_handler(db: AsyncSession, session: VoiceSession, text: str,
                       knowledge: list[dict] | None = None,
                       knowledge_note: str = "") -> str:
    """The interaction-layer handler convention, over the voice conversation."""
    from .executor import execute_workflow
    from .interactions import _extract_reply

    if not session.handler_workflow_id:
        return ""
    wf = await db.get(Workflow, session.handler_workflow_id)
    if wf is None:
        raise VoiceError("the bound handler workflow no longer exists")
    history = await _conv_messages(db, session.conversation_id) if session.conversation_id else []
    tail = [{"role": m.role, "channel": m.channel, "text": m.text} for m in history[-10:]]
    # v71: the VoiceAgent's persona rides the envelope so AI handlers speak
    # with the agent's voice (the scaffold reads metadata.system_prompt)
    # v72: knowledge matches ride metadata.knowledge so handlers answer
    # from the bound dataset (dataset-backed answers over the phone)
    agent = (session.context or {}).get("voice_agent") or {}
    metadata = {"provider": session.provider, "call_ref": session.call_ref,
                "voice_agent_id": agent.get("voice_agent_id") or "",
                "system_prompt": agent.get("system_prompt") or ""}
    if knowledge:
        metadata["knowledge"] = knowledge
    if knowledge_note:
        metadata["knowledge_error"] = knowledge_note
    envelope = {
        "conversation_id": session.conversation_id,
        "channel": "voice",
        "voice_session_id": session.id,
        "participant": {"id": session.from_ref, "name": ""},
        "text": text,
        "history": tail,
        "metadata": metadata,
    }
    result = await execute_workflow(
        session.handler_workflow_id, trigger_type="webhook",
        trigger_payload={"payload": envelope}, trigger_node_id=None)
    if result.get("status") != "success":
        raise VoiceError(f"handler workflow failed: {result.get('error') or result.get('status')}")
    last_output = result["node_runs"][-1].get("output") if result.get("node_runs") else None
    return _extract_reply(last_output)


async def voice_turn(db: AsyncSession, session: VoiceSession, *, transcript: str,
                     confidence: float = 1.0, language: str = "",
                     tts_provider: str | None = None, voice: str | None = None,
                     tts_format: str | None = None) -> dict:
    """One conversational turn: ASR result -> handler -> TTS contract.

    Requires an in_progress (or on_hold refused) session with an answered
    call. The caller's words are recorded on the LINKED conversation
    (channel=voice), the handler replies through the SAME conversation,
    and the reply comes back wrapped in the TTS contract + an interruptible
    tts.started utterance (barge-in cancels it). TTS parameters (v71)
    default to the session's VoiceAgent config when not given explicitly -
    explicit parameters still win.
    """
    if session.state == "ended":
        raise VoiceError("the call already ended")
    if session.state != "in_progress":
        raise VoiceError(f"voice turns need an in_progress call, got {session.state}")
    asr = validate_asr_result({"transcript": transcript, "confidence": confidence,
                               "language": language, "is_final": True})
    if not session.conversation_id:
        raise VoiceError("session has no linked conversation")

    # record the caller's words on the shared transcript (no re-run of the
    # conversation handler - the voice session owns its own turn execution)
    from ..models import InteractionMessage
    db.add(InteractionMessage(conversation_id=session.conversation_id, role="user",
                              channel="voice", text=asr["transcript"][:20000],
                              payload={"confidence": asr["confidence"],
                                       "language": asr["language"],
                                       "voice_session_id": session.id}))
    await db.flush()

    event = await _add_event(db, session, "asr.final",
                             {"transcript": asr["transcript"],
                              "confidence": asr["confidence"],
                              "language": asr["language"]})
    # COMMIT before the flow runs: execute_workflow writes on its own
    # sessions and SQLite allows a single writer - holding this request's
    # write transaction across the handler deadlocks the database (the
    # same discipline interactions.ingest learned in v68).
    await db.commit()

    # v72: ground the turn on the agent's knowledge binding BEFORE the
    # handler runs - matches ride the envelope's metadata.knowledge. A live
    # call must not die because the binding broke (dataset deleted
    # mid-call): the failure is recorded honestly and the handler runs
    # ungrounded (the same honesty as asr.unavailable).
    knowledge: list[dict] = []
    knowledge_note = ""
    kb = ((session.context or {}).get("voice_agent") or {}).get("knowledge") or {}
    if kb.get("dataset_id"):
        from . import knowledge as knowledge_svc
        try:
            kb_res = await knowledge_svc.knowledge_search(
                db, dataset_id=kb["dataset_id"], query=asr["transcript"],
                text_column=kb.get("text_column") or "",
                answer_column=kb.get("answer_column") or None,
                top_k=int(kb.get("top_k") or 1), owner_id=session.owner_id)
            knowledge = kb_res["matches"]
        except knowledge_svc.KnowledgeError as exc:
            knowledge_note = str(exc)

    reply = await _run_handler(db, session, asr["transcript"],
                               knowledge=knowledge, knowledge_note=knowledge_note)
    if reply:
        db.add(InteractionMessage(conversation_id=session.conversation_id, role="agent",
                                  channel="voice", text=reply[:20000],
                                  payload={"via": "voice_turn",
                                           "handler_workflow_id": session.handler_workflow_id}))
        await db.flush()

    # v71: the TTS configuration - explicit parameter -> the session's
    # VoiceAgent config -> the historical defaults
    from .voice_agents import resolve_turn_tts
    eff_tts_provider, eff_voice, eff_tts_format = resolve_turn_tts(
        session, tts_provider=tts_provider, voice=voice, tts_format=tts_format)
    tts_request = build_tts_request(reply or "", provider=eff_tts_provider,
                                    voice=eff_voice, fmt=eff_tts_format) if reply else None
    if tts_request is not None:
        tts_event = await _add_event(db, session, "tts.started",
                                     {"text": reply[:500], "provider": eff_tts_provider,
                                      "voice": eff_voice, "barge_in_ok": tts_request["barge_in_ok"]})
        ctx = dict(session.context or {})
        ctx["active_tts"] = tts_event.id
        session.context = ctx
        db.add(session)
        tts_request["tts_id"] = tts_event.id
    await db.flush()
    return {"event": _event_out(event), "asr": asr, "reply": reply or None,
            "knowledge": knowledge or None, "knowledge_error": knowledge_note or None,
            "tts": tts_request, "state": session.state}


async def complete_tts(db: AsyncSession, session: VoiceSession, *, cancelled: bool = False) -> dict:
    """Close the active utterance (played out - barge-in is the other path)."""
    active_id = (session.context or {}).get("active_tts")
    if not active_id:
        raise VoiceError("no active TTS utterance to complete")
    ctx = dict(session.context or {})
    ctx.pop("active_tts", None)
    session.context = ctx
    db.add(session)
    await _add_event(db, session, "tts.ended", {"tts_id": active_id, "cancelled": bool(cancelled)})
    await db.flush()
    return {"state": session.state, "completed": active_id}


async def hangup(db: AsyncSession, session: VoiceSession, *, reason: str = "hangup") -> dict:
    """End the call (the polite wrapper over apply_event('hangup'))."""
    if session.state == "ended":
        return {"state": session.state, "end_reason": session.end_reason}
    if session.state == "on_hold":
        # holding calls still end
        pass
    result = await apply_event(db, session, "hangup", {"reason": reason})
    # a hung-up call closes the active utterance implicitly
    if (session.context or {}).get("active_tts"):
        await complete_tts(db, session, cancelled=True)
    return result
