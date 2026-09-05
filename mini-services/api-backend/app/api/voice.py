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
* ``WS   /voice/sessions/{id}/media``      - v70 media transport: the
  provider streams the call's audio (base64 mulaw chunks) over a
  websocket; py8n decodes it, runs voice-activity detection, closes
  utterances, transcribes through a registered ASR engine, triggers the
  SAME turns/barge-in primitives (or honestly reports asr.unavailable)
* ``GET  /voice/contracts``                - the ASR/TTS contract shapes +
  the provider mapping tables + the media transport spec (derived,
  nothing stored)
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import decode_token, get_optional_user
from ..config import settings
from ..db import get_db
from ..models import VoiceSession
from ..services import voice as voice_svc
from ..services import voice_agents as agent_svc
from ..services import voice_transport as transport
from ..services.voice import VoiceError

router = APIRouter(prefix="/voice", tags=["voice"])


def _http(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


# ---------------------------------------------------------------------------
# v71: voice agents - the builder object over the voice primitives
# ---------------------------------------------------------------------------


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=140)
    description: str = Field(default="", max_length=2000)
    greeting_text: str = Field(default="", max_length=4000, description="spoken the moment the call is answered (interruptible)")
    asr_provider: str = Field(default="py8n_local", max_length=40, description="openai_whisper | deepgram | assemblyai | py8n_local")
    tts_provider: str = Field(default="openai_tts", max_length=40, description="openai_tts | elevenlabs | piper_local | meta_mms")
    tts_voice: str = Field(default="alloy", max_length=80)
    tts_format: str = Field(default="wav", max_length=10)
    language: str = Field(default="en-US", max_length=20)
    barge_in: bool = Field(default=True, description="may the caller interrupt the greeting and turns")
    system_prompt: str = Field(default="", max_length=8000, description="the persona injected into the handler envelope's metadata")
    handler_workflow_id: str | None = None
    scaffold_handler: bool = Field(default=False, description="scaffold a runnable trigger -> code handler when none is bound")
    knowledge_dataset_id: str | None = Field(default=None, max_length=36, description="v72: bind a dataset - every turn is grounded on its rows")
    knowledge_text_column: str | None = Field(default=None, max_length=80, description="the question/text column (default: the dataset's first column)")
    knowledge_answer_column: str | None = Field(default=None, max_length=80, description="the answer column (default: the text column)")
    knowledge_top_k: int = Field(default=1, ge=1, le=5, description="how many knowledge matches ride the handler envelope")
    brain: str = Field(default="scaffold", max_length=20, description="v73: scaffold (echo code node) | ai_agent (LLM brain grounded on the SAME knowledge binding)")
    brain_provider: str = Field(default="sandbox_bridge", max_length=40, description="v73: sandbox_bridge | openai_compatible")
    brain_model: str = Field(default="", max_length=120, description="v73: optional model name for the brain provider")


@router.post("/agents", status_code=201)
async def create_agent(body: AgentCreate, user=Depends(get_optional_user),
                       db: AsyncSession = Depends(get_db)):
    try:
        return await agent_svc.create_agent(
            db, owner_id=getattr(user, "id", None), name=body.name,
            description=body.description, greeting_text=body.greeting_text,
            asr_provider=body.asr_provider, tts_provider=body.tts_provider,
            tts_voice=body.tts_voice, tts_format=body.tts_format,
            language=body.language, barge_in=body.barge_in,
            system_prompt=body.system_prompt,
            handler_workflow_id=body.handler_workflow_id,
            scaffold_handler=body.scaffold_handler,
            knowledge_dataset_id=body.knowledge_dataset_id,
            knowledge_text_column=body.knowledge_text_column,
            knowledge_answer_column=body.knowledge_answer_column,
            knowledge_top_k=body.knowledge_top_k,
            brain=body.brain, brain_provider=body.brain_provider,
            brain_model=body.brain_model)
    except agent_svc.VoiceAgentError as exc:
        raise _http(exc) from exc


@router.get("/agents")
async def list_agents(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    return {"agents": await agent_svc.list_agents(db, getattr(user, "id", None))}


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, user=Depends(get_optional_user),
                    db: AsyncSession = Depends(get_db)):
    try:
        return await agent_svc.get_agent(db, agent_id, getattr(user, "id", None))
    except agent_svc.VoiceAgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=140)
    description: str | None = Field(default=None, max_length=2000)
    greeting_text: str | None = Field(default=None, max_length=4000)
    asr_provider: str | None = Field(default=None, max_length=40)
    tts_provider: str | None = Field(default=None, max_length=40)
    tts_voice: str | None = Field(default=None, max_length=80)
    tts_format: str | None = Field(default=None, max_length=10)
    language: str | None = Field(default=None, max_length=20)
    barge_in: bool | None = None
    system_prompt: str | None = Field(default=None, max_length=8000)
    handler_workflow_id: str | None = None
    knowledge_dataset_id: str | None = Field(default=None, max_length=36,
                                             description="v72: non-empty binds/rewires, empty string clears")
    knowledge_text_column: str | None = Field(default=None, max_length=80)
    knowledge_answer_column: str | None = Field(default=None, max_length=80)
    knowledge_top_k: int | None = Field(default=None, ge=1, le=5)
    brain: str | None = Field(default=None, max_length=20, description="v73: flip the brain (scaffold <-> ai_agent); re-scaffolds a scaffolded handler, refuses on a custom one")
    brain_provider: str | None = Field(default=None, max_length=40)
    brain_model: str | None = Field(default=None, max_length=120)


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, body: AgentUpdate,
                       user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    try:
        return await agent_svc.update_agent(
            db, agent_id, getattr(user, "id", None),
            name=body.name, description=body.description,
            greeting_text=body.greeting_text, asr_provider=body.asr_provider,
            tts_provider=body.tts_provider, tts_voice=body.tts_voice,
            tts_format=body.tts_format, language=body.language,
            barge_in=body.barge_in, system_prompt=body.system_prompt,
            handler_workflow_id=body.handler_workflow_id,
            knowledge_dataset_id=body.knowledge_dataset_id,
            knowledge_text_column=body.knowledge_text_column,
            knowledge_answer_column=body.knowledge_answer_column,
            knowledge_top_k=body.knowledge_top_k,
            brain=body.brain, brain_provider=body.brain_provider,
            brain_model=body.brain_model)
    except agent_svc.VoiceAgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, user=Depends(get_optional_user),
                       db: AsyncSession = Depends(get_db)):
    try:
        return await agent_svc.delete_agent(db, agent_id, getattr(user, "id", None))
    except agent_svc.VoiceAgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# v72: knowledge binding - dataset-backed answers over the phone
# ---------------------------------------------------------------------------


class KnowledgeSearchBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="what a caller just said")
    top_k: int | None = Field(default=None, ge=1, le=5, description="override the agent's bound top_k")


@router.post("/agents/{agent_id}/knowledge/search")
async def knowledge_search(agent_id: str, body: KnowledgeSearchBody,
                           user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Preview what a voice turn would be grounded on (no call needed).

    Runs the exact retrieval the turn loop runs against the agent's bound
    dataset - matches carry score + row evidence, so wiring is testable
    before a single phone rings.
    """
    from ..services import knowledge as knowledge_svc
    from ..services.knowledge import KnowledgeError

    try:
        agent = await agent_svc.get_agent(db, agent_id, getattr(user, "id", None))
    except agent_svc.VoiceAgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    kb = agent.get("knowledge") or {}
    if not kb.get("dataset_id"):
        raise HTTPException(status_code=409, detail="this agent has no knowledge dataset bound - "
                                                    "set knowledge_dataset_id on the agent first")
    try:
        return await knowledge_svc.knowledge_search(
            db, dataset_id=kb["dataset_id"], query=body.query,
            text_column=kb.get("text_column") or "",
            answer_column=kb.get("answer_column") or None,
            top_k=body.top_k or int(kb.get("top_k") or 1),
            owner_id=getattr(user, "id", None))
    except KnowledgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# v73: voice session analytics - per-turn ASR confidence trends (derived)
# ---------------------------------------------------------------------------


@router.get("/agents/{agent_id}/analytics")
async def agent_voice_analytics(agent_id: str, user=Depends(get_optional_user),
                                db: AsyncSession = Depends(get_db)):
    """ASR confidence analytics pooled across an agent's sessions.

    Per-session confidence summaries + trend directions, pooled statistics
    and the weak-turn rate - derived from each session's asr.final events
    at read time, never stored.
    """
    from ..services import voice_analytics

    try:
        return await voice_analytics.agent_analytics(db, agent_id, getattr(user, "id", None))
    except agent_svc.VoiceAgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/analytics")
async def session_voice_analytics(session_id: str, user=Depends(get_optional_user),
                                  db: AsyncSession = Depends(get_db)):
    """Per-turn ASR confidence analytics for ONE call.

    The turn-by-turn confidence series, summary statistics, weak turns
    (below the 0.6 gate) and a least-squares trend over the turn indices -
    is the caller being understood better or worse as the call goes on?
    """
    from ..services import voice_analytics

    row = await _own_session(db, session_id, user)
    return await voice_analytics.session_analytics(db, row)


# ---------------------------------------------------------------------------
# v72: live speech engine bridges - the honest machine inventory + TTS
# ---------------------------------------------------------------------------


@router.get("/speech/engines")
async def speech_engines():
    """Which local ASR/TTS bridges can actually run on this machine.

    The devices.py pattern, for speech: availability is probed (binary +
    model), never assumed - a missing bridge reports exact remediation and
    the transport keeps reporting asr.unavailable for it.
    """
    from ..services import speech_engines as speech_svc

    return speech_svc.speech_inventory()


# ---------------------------------------------------------------------------
# v73: real model installs - the fully offline phone
# ---------------------------------------------------------------------------


@router.get("/speech/models")
async def speech_models():
    """The curated model catalog + what is already on this machine."""
    from ..services import speech_engines as speech_svc
    from ..services import speech_models

    inv = speech_svc.speech_inventory()
    out = speech_models.catalog_out()
    out["inventory"] = inv
    out["offline_phone"] = {
        "asr_local": bool(inv["asr"]["local_engine_registered"]),
        "tts_local": bool(inv["tts"]["local_engine_registered"]),
        "ready": bool(inv["asr"]["local_engine_registered"]
                      and inv["tts"]["local_engine_registered"]),
        "note": "ready = the whole loop (in-process ASR, handler, local TTS) runs "
                "on this machine with no cloud dependency",
    }
    return out


class ModelInstallBody(BaseModel):
    slug: str = Field(..., min_length=1, max_length=80,
                      description="a catalog slug (GET /voice/speech/models)")


@router.post("/speech/models/install")
async def install_speech_model(body: ModelInstallBody, user=Depends(get_optional_user)):
    """Download + verify + install a REAL speech model, then re-bind.

    Streams the artifact to <dest>.part (atomic), verifies it (zip CRC +
    Kaldi layout / ggml magic / onnx+json pair), lays it out exactly where
    the probes look and re-runs bind_local_engines() - a bare machine gains
    its offline ASR/TTS in one call. Downloads are blocking and honest:
    the response lands when the bytes are on disk.
    """
    from ..services import speech_engines as speech_svc
    from ..services import speech_models
    from ..services.speech_models import SpeechModelError

    try:
        result = await asyncio.to_thread(speech_models.install_model, body.slug.strip())
    except SpeechModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - network errors surface honestly
        raise HTTPException(status_code=502,
                            detail=f"download failed: {type(exc).__name__}: {exc}") from exc
    bound = await asyncio.to_thread(speech_svc.bind_local_engines)
    return {"install": result, "bound": bound,
            "inventory": speech_svc.speech_inventory()}


class TTSBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000)
    provider: str = Field(default="piper_local", max_length=40,
                          description="the registered engine to use (piper_local)")
    voice: str = Field(default="", max_length=80)
    format: str = Field(default="wav", max_length=10)


@router.post("/tts/synthesize")
async def tts_synthesize(body: TTSBody, user=Depends(get_optional_user)):
    """Synthesize text through a REGISTERED local TTS engine (v72).

    Returns the v69 TTS contract result (audio_b64 + honest duration).
    Hosted providers (openai_tts / elevenlabs) are executed by their own
    bridges at delivery time - this endpoint runs the in-process ones.
    """
    from ..services import speech_engines
    from ..services.voice import VoiceError

    try:
        result = speech_engines.synthesize(body.provider, body.text,
                                           voice=body.voice, fmt=body.format)
        result["provider"] = body.provider
        return result
    except VoiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    agent_id: str | None = Field(default=None, description="VoiceAgent whose greeting/speech config this call inherits (v71)")


@router.post("/sessions", status_code=201)
async def create_session(body: SessionCreate, user=Depends(get_optional_user),
                         db: AsyncSession = Depends(get_db)):
    try:
        return await voice_svc.create_session(
            db, owner_id=getattr(user, "id", None), direction=body.direction,
            provider=body.provider, call_ref=body.call_ref, from_ref=body.from_ref,
            to_ref=body.to_ref, handler_workflow_id=body.handler_workflow_id,
            conversation_ref=body.conversation_ref, agent_id=body.agent_id)
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
        out = await voice_svc.apply_event(db, row, body.kind, body.payload)
        # v71: the agent's greeting rides call.answered - an interruptible
        # tts.started built from the agent's own TTS configuration
        greeting_tts = None
        if body.kind == "call.answered":
            from ..services.voice_agents import on_answered
            greeting_tts = await on_answered(db, row)
            if greeting_tts is not None:
                await db.commit()
        out["greeting_tts"] = greeting_tts
        return out
    except VoiceError as exc:
        raise _http(exc) from exc


class TurnBody(BaseModel):
    transcript: str = Field(..., min_length=1, max_length=8000)
    confidence: float = Field(default=1.0, ge=0, le=1)
    language: str = Field(default="", max_length=20)
    tts_provider: str | None = Field(default=None, max_length=40, description="default: the session's VoiceAgent config, else openai_tts")
    voice: str | None = Field(default=None, max_length=80, description="default: the session's VoiceAgent config, else alloy")
    tts_format: str | None = Field(default=None, max_length=10, description="default: the session's VoiceAgent config, else wav")


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
        "media": {"events": list(transport.MEDIA_EVENTS),
                  "encodings": list(transport.AUDIO_ENCODINGS),
                  "sample_rate": transport.MEDIA_SAMPLE_RATE,
                  "asr_engines_registered": transport.registered_asr_engines()},
    }


# ---------------------------------------------------------------------------
# v70: the media transport - providers push call audio over a websocket
# ---------------------------------------------------------------------------

# Websockets cannot send Authorization headers on the handshake (and media
# streams are PROVIDER-facing, not browser-facing), so this router is
# included WITHOUT the enforced gate and authenticates like ws.py: the
# JWT rides ?token= when the platform runs enforced; provider streams pass
# their own credentials via the stream's customParameters instead.
media_router = APIRouter(prefix="/voice", tags=["voice-media"])


@media_router.websocket("/sessions/{session_id}/media")
async def media_stream(websocket: WebSocket, session_id: str):
    """The call's audio, streamed in.

    Protocol (JSON frames, the de-facto provider dialect):

    client -> server: ``{event: "start", start: {streamSid, callSid,
    customParameters}}``, ``{event: "media", media: {payload: <base64
    mulaw/linear16>, track, chunk}}``, ``{event: "mark", ...}``,
    ``{event: "stop", ...}``.

    server -> client: ``connected`` on open; ``speech.started`` /
    ``speech.ended`` as the VAD closes utterances; ``asr.final`` with the
    transcript (when an ASR engine is registered) or ``asr.unavailable``
    (honest - the transport never invents words); ``turn`` with the TTS
    contract result of the handler run; ``barge_in`` when speech starts
    over an active utterance; ``stream_stopped`` with the final counters.

    The voice session owns all the semantics: media events run through the
    SAME state machine, turns run the SAME handler workflow, barge-in is
    the SAME primitive the HTTP endpoint exposes.
    """
    token = websocket.query_params.get("token") or ""
    user_id = decode_token(token) if token else None
    if settings.require_auth and user_id is None:
        await websocket.close(code=4401)  # unauthenticated
        return

    from ..db import AsyncSessionLocal
    from ..services.voice import VoiceError

    async with AsyncSessionLocal() as db:
        session = await db.get(VoiceSession, session_id)
        if session is None:
            await websocket.close(code=4404)
            return
        owner_id = session.owner_id
    if user_id is not None and owner_id is not None and owner_id != user_id:
        await websocket.close(code=4404)  # looks nonexistent
        return

    await websocket.accept()
    # v71: the session may carry a VoiceAgent - the connected frame reports
    # the binding and the engine the stream will consult (honest: whether
    # it is actually registered in THIS process)
    agent_cfg = ((session.context or {}).get("voice_agent") or {}) if session else {}
    engine_name = str(agent_cfg.get("asr_provider") or "py8n_local")
    await websocket.send_text(json.dumps({
        "event": "connected", "protocol": "py8n-media", "version": settings.version,
        "session_id": session_id, "state": session.state,
        "asr_engines": transport.registered_asr_engines(),
        "asr_engine": engine_name,
        "asr_engine_registered": engine_name in transport.registered_asr_engines(),
        "agent": ({"id": agent_cfg.get("voice_agent_id"), "name": agent_cfg.get("voice_agent_name"),
                   "barge_in": bool(agent_cfg.get("barge_in", True))}
                  if agent_cfg else None),
    }))

    stats = transport.MediaStreamStats()
    segmenter = transport.UtteranceSegmenter()
    stream_open = False
    chunks_since_flush = 0
    # the start frame's customParameters (encoding, sample_rate, asr_engine...)
    # govern the WHOLE stream - providers send them once at fork start
    stream_custom_params: dict = {}

    async def _load_session() -> VoiceSession | None:
        async with AsyncSessionLocal() as db:
            return await db.get(VoiceSession, session_id)

    async def _record(kind: str, payload: dict, *, apply: bool = False) -> VoiceSession | None:
        """One event on the session, on its own short-lived write."""
        async with AsyncSessionLocal() as db:
            row = await db.get(VoiceSession, session_id)
            if row is None:
                return None
            try:
                if apply:
                    await voice_svc.apply_event(db, row, kind, payload)
                else:
                    await voice_svc._add_event(db, row, kind, payload)
                    await db.commit()
            except VoiceError:
                await db.rollback()
                return None
            return row

    async def _save_media_context(**extra) -> None:
        async with AsyncSessionLocal() as db:
            row = await db.get(VoiceSession, session_id)
            if row is None:
                return
            ctx = dict(row.context or {})
            media_ctx = dict(ctx.get("media") or {})
            media_ctx.update(stats.snapshot())
            media_ctx.update(extra)
            ctx["media"] = media_ctx
            row.context = ctx
            db.add(row)
            await db.commit()

    async def _send(frame: dict) -> bool:
        try:
            await websocket.send_text(json.dumps(frame, default=str))
            return True
        except Exception:  # noqa: BLE001 - client vanished mid-stream
            return False

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                frame_in = json.loads(raw)
            except (ValueError, TypeError):
                frame_in = None
            frame, skip = transport.parse_media_frame(frame_in) if frame_in is not None \
                else (None, {"reason": "bad_json", "detail": "frame was not JSON"})
            if frame is None:
                stats.skipped_frames += 1
                if not await _send({"event": "skipped", **(skip or {})}):
                    break
                continue

            if frame.event == "start":
                stream_open = True
                stats.stream_sid = frame.stream_sid
                stream_custom_params = dict(frame.custom_parameters or {})
                await _record("media.stream_started",
                              {"stream_sid": frame.stream_sid, "call_ref": frame.call_ref,
                               "encoding": frame.encoding, "sample_rate": frame.sample_rate,
                               "asr_engine": stream_custom_params.get("asr_engine") or None})
                await _save_media_context(opened=True)
                await _send({"event": "stream_started", "stream_sid": frame.stream_sid,
                             "state": session.state})
                continue

            if frame.event == "media":
                pcm, duration_ms, dskip = transport.decode_audio_chunk(frame)
                if dskip is not None:
                    stats.skipped_frames += 1
                    await _send({"event": "skipped", **dskip})
                    continue
                stats.chunks += 1
                stats.audio_bytes += len(frame.payload_b64) * 3 // 4  # base64 ratio
                stats.audio_ms += duration_ms
                chunks_since_flush += 1

                for seg in segmenter.feed(pcm, duration_ms):
                    if seg.kind == "speech.started":
                        row = await _load_session()
                        if row and (row.context or {}).get("active_tts") and row.state != "ended":
                            # THE barge-in trigger: the caller spoke over the agent
                            async with AsyncSessionLocal() as db:
                                live = await db.get(VoiceSession, session_id)
                                try:
                                    result = await voice_svc.barge_in(db, live)
                                    await db.commit()
                                except VoiceError:
                                    await db.rollback()
                                    result = None
                            if result:
                                await _send({"event": "barge_in", **result})
                        await _record("speech.started", {"start_ms": seg.start_ms})
                        await _send({"event": "speech.started", "segment": seg.out()})
                    else:  # speech.ended - the utterance is complete
                        await _record("speech.ended", {"start_ms": seg.start_ms,
                                                       "end_ms": seg.end_ms,
                                                       "duration_ms": seg.duration_ms})
                        await _send({"event": "speech.ended", "segment": seg.out()})
                        # v71: engine resolution - the start frame's
                        # customParameters win, else the session's VoiceAgent,
                        # else py8n_local
                        live_row = await _load_session()
                        engine_name = agent_svc.resolve_asr_engine_name(
                            live_row, stream_custom_params)
                        engine = transport.get_asr_engine(engine_name)
                        if engine is None:
                            await _send({"event": "asr.unavailable",
                                         "detail": f"no ASR engine is registered for "
                                                   f"{engine_name!r} in this process - bind one with "
                                                   "voice_transport.register_asr_engine; "
                                                   "the utterance's audio was measured, "
                                                   "not transcribed",
                                         "segment": seg.out()})
                        else:
                            try:
                                # v72: real engines (vosk/whisper.cpp) do actual
                                # compute - run the sync callable off the event
                                # loop so the stream keeps flowing
                                asr_raw = await asyncio.to_thread(engine, seg.pcm, frame.sample_rate)
                                asr = voice_svc.validate_asr_result(asr_raw)
                            except voice_svc.VoiceError as exc:
                                await _send({"event": "asr.error", "detail": str(exc),
                                             "segment": seg.out()})
                                continue
                            await _send({"event": "asr.final", "asr": asr,
                                         "segment": seg.out()})
                            # the turn ONLY runs when the transport knows how to
                            # drive it: session in_progress + handler bound
                            async with AsyncSessionLocal() as db:
                                live = await db.get(VoiceSession, session_id)
                                if live is None or live.state != "in_progress":
                                    await _send({"event": "turn.skipped",
                                                 "detail": f"voice turns need an "
                                                           f"in_progress call, got "
                                                           f"{live.state if live else 'gone'}"})
                                    continue
                                try:
                                    turn = await voice_svc.voice_turn(
                                        db, live, transcript=asr["transcript"],
                                        confidence=asr["confidence"],
                                        language=asr["language"])
                                    await db.commit()
                                except VoiceError as exc:
                                    await db.rollback()
                                    turn = {"error": str(exc)}
                            # spread the turn MINUS its internal "event" dict -
                            # the protocol frame's event key must stay "turn"
                            turn_frame = {k: v for k, v in turn.items() if k != "event"}
                            await _send({"event": "turn", **turn_frame})

                if chunks_since_flush >= 25:
                    chunks_since_flush = 0
                    await _save_media_context()
                continue

            if frame.event == "mark":
                await _send({"event": "mark", "name": frame.mark_name})
                continue

            if frame.event == "stop":
                for seg in segmenter.flush():
                    if seg.kind == "speech.ended":
                        await _record("speech.ended", {"start_ms": seg.start_ms,
                                                       "end_ms": seg.end_ms,
                                                       "flushed": True,
                                                       "duration_ms": seg.duration_ms})
                await _record("media.stream_stopped", {**stats.snapshot()})
                await _save_media_context(opened=False, stopped=True)
                await _send({"event": "stream_stopped", "stats": stats.snapshot()})
                break

            # "connected" frames from the provider side: acknowledged, nothing stored
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if stream_open:
            try:
                await _record("media.stream_stopped", {**stats.snapshot(), "aborted": True})
            except Exception:  # noqa: BLE001 - socket teardown best-effort
                pass
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
