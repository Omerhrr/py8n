"""Voice API (v69) - call primitives over HTTP.

* ``POST /voice/sessions``                 - open a call (inbound|outbound),
  linked to (or opening) an interaction-layer conversation
* ``GET  /voice/sessions``                 - the call list (state filter)
* ``GET  /voice/sessions/{id}``            - one call: state machine state,
  derived duration / barge-in count / turn count, full event timeline
* ``POST /voice/sessions/{id}/events``     - run one event through the
  state machine (ringing / answered / hold / dtmf / no_answer / hangup...)
* ``POST /voice/sessions/{id}/turn``       - a voice TURN: ASR result ->
  handler workflow -> TTS contract (interruptible tts.started)
* ``POST /voice/sessions/{id}/barge-in``   - the caller interrupts the
  agent; the active utterance is cancelled and counted
* ``POST /voice/sessions/{id}/tts/complete`` - the utterance played out
* ``POST /voice/webhooks/twilio/{id}``     - Twilio call-status callback
  translated into session events (the first real voice provider adapter)
* ``GET  /voice/contracts``                - the ASR/TTS contract shapes +
  the provider mapping tables (derived, nothing stored)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..models import VoiceSession
from ..services import voice as voice_svc
from ..services.voice import VoiceError

router = APIRouter(prefix="/voice", tags=["voice"])


def _http(exc: VoiceError, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


async def _own_session(db: AsyncSession, session_id: str, user) -> VoiceSession:
    row = await db.get(VoiceSession, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    if user is not None and row.owner_id is not None and row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    return row


class SessionCreate(BaseModel):
    direction: str = Field(default="inbound", description="inbound | outbound")
    provider: str = Field(default="twilio", max_length=40)
    call_ref: str = Field(default="", max_length=180, description="Provider call id (CallSid...)")
    from_ref: str = Field(default="", max_length=180)
    to_ref: str = Field(default="", max_length=180)
    handler_workflow_id: str | None = None
    conversation_ref: str | None = Field(default=None, description="Attach the call to an existing conversation (channel hop continuity)")


@router.post("/sessions", status_code=201)
async def create_session(body: SessionCreate, user=Depends(get_optional_user),
                         db: AsyncSession = Depends(get_db)):
    try:
        return await voice_svc.create_session(
            db, owner_id=getattr(user, "id", None), direction=body.direction,
            provider=body.provider, call_ref=body.call_ref, from_ref=body.from_ref,
            to_ref=body.to_ref, handler_workflow_id=body.handler_workflow_id,
            conversation_ref=body.conversation_ref)
    except VoiceError as exc:
        raise _http(exc) from exc


@router.get("/sessions")
async def list_sessions(state: str | None = None, limit: int = 100,
                        user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    return {"sessions": await voice_svc.list_sessions(
        db, getattr(user, "id", None), state=state, limit=limit)}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, user=Depends(get_optional_user),
                      db: AsyncSession = Depends(get_db)):
    out = await voice_svc.get_session(db, session_id, getattr(user, "id", None))
    if out is None:
        raise HTTPException(status_code=404, detail="Not found")
    return out


class EventBody(BaseModel):
    kind: str = Field(..., description="call.ringing | call.answered | hold | unhold | dtmf | transfer | no_answer | busy | voicemail_detected | hangup | failed")
    payload: dict = Field(default_factory=dict)


@router.post("/sessions/{session_id}/events")
async def apply_event(session_id: str, body: EventBody,
                      user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _own_session(db, session_id, user)
    try:
        return await voice_svc.apply_event(db, row, body.kind, body.payload)
    except VoiceError as exc:
        raise _http(exc) from exc


class TurnBody(BaseModel):
    transcript: str = Field(..., min_length=1, max_length=8000)
    confidence: float = Field(default=1.0, ge=0, le=1)
    language: str = Field(default="", max_length=20)
    tts_provider: str = Field(default="openai_tts", max_length=40)
    voice: str = Field(default="alloy", max_length=80)
    tts_format: str = Field(default="wav", max_length=10)


@router.post("/sessions/{session_id}/turn")
async def voice_turn(session_id: str, body: TurnBody,
                     user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _own_session(db, session_id, user)
    try:
        return await voice_svc.voice_turn(
            db, row, transcript=body.transcript, confidence=body.confidence,
            language=body.language, tts_provider=body.tts_provider,
            voice=body.voice, tts_format=body.tts_format)
    except VoiceError as exc:
        raise _http(exc) from exc


@router.post("/sessions/{session_id}/barge-in")
async def barge_in(session_id: str, user=Depends(get_optional_user),
                   db: AsyncSession = Depends(get_db)):
    row = await _own_session(db, session_id, user)
    try:
        return await voice_svc.barge_in(db, row)
    except VoiceError as exc:
        raise _http(exc) from exc


@router.post("/sessions/{session_id}/tts/complete")
async def complete_tts(session_id: str, user=Depends(get_optional_user),
                       db: AsyncSession = Depends(get_db)):
    row = await _own_session(db, session_id, user)
    try:
        return await voice_svc.complete_tts(db, row)
    except VoiceError as exc:
        raise _http(exc) from exc


@router.post("/webhooks/twilio/{session_id}")
async def twilio_status(session_id: str, request: Request,
                        user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Twilio call-status callbacks -> session events (provider adapter #1)."""
    row = await _own_session(db, session_id, user)
    form: dict = {}
    ctype = request.headers.get("content-type", "")
    if "form" in ctype:
        form = {k: v for k, v in (await request.form()).items()}
    else:
        try:
            form = dict(await request.json() or {})
        except Exception:  # noqa: BLE001 - twilio sends x-www-form-urlencoded
            form = {}
    try:
        _call_ref, kind, extra = voice_svc.parse_twilio_status(form)
        if _call_ref and not row.call_ref:
            row.call_ref = _call_ref
            db.add(row)
        return await voice_svc.apply_event(db, row, kind, extra)
    except VoiceError as exc:
        raise _http(exc) from exc


@router.get("/contracts")
async def contracts():
    """The ASR/TTS contracts + provider maps - the speech adapter spec."""
    return {
        "asr": {"contract": voice_svc.ASR_CONTRACT, "providers": voice_svc.ASR_PROVIDERS},
        "tts": {"contract": voice_svc.TTS_CONTRACT, "providers": voice_svc.TTS_PROVIDERS},
        "call_states": list(voice_svc.STATES),
        "transitions": {k: sorted(v) for k, v in voice_svc._TRANSITIONS.items()},
        "event_kinds": list(voice_svc.EVENT_KINDS),
    }
