"""Media API (v80) - the MediaSession runtime over HTTP.

The one door to any real-time session, whatever modality it carries:

* ``POST /media/sessions``           - open one: kind=voice wraps a call,
                                       kind=video opens a video-first room,
                                       kind=meeting opens an audio room
* ``GET  /media/sessions``           - the runtime list (kind/state filters)
* ``GET  /media/sessions/{id}``      - the MediaSession projection:
                                       participants, audio/video/screen
                                       tracks, data channels, permissions,
                                       presence, events, recording,
                                       transcription, state
* ``POST /media/sessions/{id}/participants`` - add a leg (rooms)
* ``POST /media/sessions/{id}/end``  - end the session and the primitive
                                       it wraps
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..services import media_runtime as runtime
from ..services.media_runtime import MediaRuntimeError
from ..services.voice_meetings import VoiceMeetingError
from ..services.voice_agents import VoiceAgentError
from ..services.voice import VoiceError

router = APIRouter(prefix="/media", tags=["media"])


def _http(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


class MediaSessionCreate(BaseModel):
    kind: str = Field(..., description="voice | video | meeting")
    title: str = Field(default="", max_length=200)
    agent_id: str | None = Field(default=None, max_length=36,
                                 description="the room persona (video/meeting kinds)")
    session_id: str | None = Field(default=None, max_length=36,
                                   description="the call to wrap (kind=voice)")
    meeting_id: str | None = Field(default=None, max_length=36,
                                   description="an existing room to wrap (video/meeting)")


class ParticipantAdd(BaseModel):
    label: str = Field(..., min_length=1, max_length=140)
    channel: str = Field(default="web", max_length=30)
    address: str = Field(default="", max_length=180)
    endpoint_id: str | None = Field(default=None, max_length=36,
                                    description="the telnyx voice receiver for phone legs")


@router.post("/sessions", status_code=201)
async def create_media_session(body: MediaSessionCreate,
                               user=Depends(get_optional_user),
                               db: AsyncSession = Depends(get_db)):
    try:
        out = await runtime.create_media_session(
            db, getattr(user, "id", None), body.kind, title=body.title,
            agent_id=body.agent_id, session_id=body.session_id,
            meeting_id=body.meeting_id)
        await db.commit()
        return out
    except (MediaRuntimeError, VoiceMeetingError, VoiceAgentError, VoiceError) as exc:
        raise _http(exc) from exc


@router.get("/sessions")
async def list_media_sessions(kind: str | None = None, state: str | None = None,
                              limit: int = 50, user=Depends(get_optional_user),
                              db: AsyncSession = Depends(get_db)):
    try:
        return {"media_sessions": await runtime.list_media_sessions(
            db, getattr(user, "id", None), kind=kind, state=state, limit=limit)}
    except MediaRuntimeError as exc:
        raise _http(exc) from exc


@router.get("/sessions/{media_session_id}")
async def get_media_session(media_session_id: str, user=Depends(get_optional_user),
                            db: AsyncSession = Depends(get_db)):
    try:
        return await runtime.media_view(db, getattr(user, "id", None), media_session_id)
    except MediaRuntimeError as exc:
        raise _http(exc, 404) from exc


@router.post("/sessions/{media_session_id}/participants", status_code=201)
async def add_participant(media_session_id: str, body: ParticipantAdd,
                          user=Depends(get_optional_user),
                          db: AsyncSession = Depends(get_db)):
    try:
        out = await runtime.add_participant(
            db, getattr(user, "id", None), media_session_id, label=body.label,
            channel=body.channel, address=body.address,
            endpoint_id=body.endpoint_id)
        await db.commit()
        return out
    except (MediaRuntimeError, VoiceMeetingError, VoiceAgentError, VoiceError) as exc:
        raise _http(exc) from exc


@router.post("/sessions/{media_session_id}/end")
async def end_media_session(media_session_id: str, user=Depends(get_optional_user),
                            db: AsyncSession = Depends(get_db)):
    try:
        out = await runtime.end_media_session(db, getattr(user, "id", None),
                                              media_session_id)
        await db.commit()
        return out
    except MediaRuntimeError as exc:
        raise _http(exc) from exc
