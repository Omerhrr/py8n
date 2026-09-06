"""Media runtime (v80) - the real-time foundation as ONE abstraction.

v69..v79 built the real-time layers as honest primitives: calls (the
state machine + ASR/TTS + barge-in), meetings (the room, its legs, the
mix/floor/chat discipline), video (the track registry + the signaling
relay), queues, recordings. They were siblings by CONSTRUCTION but not
by INTERFACE - a client had to know which primitive it was talking to.

This module is the runtime that makes voice and video siblings:

    MediaSession
    ├── participants     (the legs, or the call's two sides)
    ├── audio tracks     (what actually flows, derived - never faked)
    ├── video tracks     (the v78 registry)
    ├── screen tracks    (the v78 registry)
    ├── data channels    (chat, speaking queue, signaling, conversation)
    ├── permissions      (mix gates + floor + barge-in)
    ├── presence         (joined / dialing / left, derived)
    ├── events           (the underlying timelines, unified)
    ├── recording        (the v79 archive handle)
    ├── transcription    (the transcript artifacts)
    └── state

    Voice Session = MediaSession + audio          (wraps a call)
    Video Session = MediaSession + audio + video  (a video-first room)
    Meeting       = MediaSession + participants   (the room)

The MediaSession row is the HANDLE (kind, title, what it wraps, state,
config); EVERYTHING under it is DERIVED at read time from the primitive
it wraps - the same derived-never-stored rule as the transcript, the
floor and the queue positions. py8n is still not the media plane: the
projection reports what flows THROUGH py8n and says so about everything
that does not.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MediaSession, VoiceEvent, VoiceMeetingMessage, \
    VoiceMeetingParticipant, VoiceSession
from .system_events import emit
from .voice_meetings import (VoiceMeetingError, _load as _load_meeting,
                             _participants as _meeting_participants,
                             floor_state_of, hand_queue_out, mix_of,
                             participant_out)
from .voice_video import VideoError, video_state

# kind -> what it wraps. A voice session wraps a CALL (VoiceSession);
# video sessions and meetings wrap a ROOM (VoiceMeeting, modality set
# in its context so the room knows what it is).
KINDS = ("voice", "video", "meeting")
REF_KIND_OF = {"voice": "session", "video": "meeting", "meeting": "meeting"}
MODALITY_OF = {"voice": "audio", "video": "audio+video", "meeting": "audio"}

MAX_VIEW_EVENTS = 30
MAX_LIST = 200


class MediaRuntimeError(ValueError):
    """Honest 4xx-grade media-runtime failures."""


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# create / load / list
# ---------------------------------------------------------------------------


async def create_media_session(db: AsyncSession, owner_id: str | None, kind: str, *,
                               title: str = "", agent_id: str | None = None,
                               session_id: str | None = None,
                               meeting_id: str | None = None) -> dict:
    """Open a real-time session through the ONE runtime door.

    * ``kind="voice"``   - wraps an EXISTING call (``session_id``): a
      carrier call or a web leg's session. A call's participants are the
      call; the runtime adds the unified view, not a second call object.
    * ``kind="video"``   - a VIDEO-FIRST room: a new meeting whose
      context.modality is audio+video (or wraps ``meeting_id``).
    * ``kind="meeting"`` - an audio-first room (or wraps ``meeting_id``).
    """
    if kind not in KINDS:
        raise MediaRuntimeError(f"kind must be {'|'.join(KINDS)}, got {kind!r}")
    ref_kind = REF_KIND_OF[kind]
    if ref_kind == "session":
        if not session_id:
            raise MediaRuntimeError(
                "kind 'voice' wraps a call - pass session_id (open one via POST /voice/sessions)")
        call = await db.get(VoiceSession, session_id)
        if call is None or (call.owner_id is not None and call.owner_id != owner_id):
            raise MediaRuntimeError(f"voice session {session_id!r} not found")
        if call.state == "ended":
            raise MediaRuntimeError("that call already ended - there is nothing to wrap")
        ref_id = call.id
        if not title:
            title = f"call {call.from_ref or call.direction} <-> {call.to_ref}".strip()[:200]
    else:
        from . import voice_meetings as meetings_svc

        if meeting_id:
            room = await _load_meeting(db, meeting_id, owner_id)
            ref_id = room.id
            if not title:
                title = room.title
        else:
            created = await meetings_svc.create_meeting(
                db, owner_id=owner_id, agent_id=agent_id, title=title)
            ref_id = created["id"]
            # the room learns its modality (context, the same place the
            # floor and the hand queue live)
            room = await _load_meeting(db, created["id"], owner_id)
            ctx = dict(room.context or {})
            ctx["modality"] = MODALITY_OF[kind]
            ctx["media_session_kind"] = kind
            room.context = ctx
            db.add(room)
            await db.flush()
    row = MediaSession(owner_id=owner_id, kind=kind, title=(title or "")[:200],
                       ref_kind=ref_kind, ref_id=ref_id, agent_id=agent_id or None,
                       state="active", config={})
    db.add(row)
    await db.flush()
    await emit(db, owner_id, "media.session_started", source="media",
               actor=owner_id or "system", target_type="media_session",
               target_id=row.id,
               payload={"kind": kind, "ref_kind": ref_kind, "ref_id": ref_id,
                        "title": row.title},
               correlation_id=row.id)
    return await media_view(db, owner_id, row.id)


# ---------------------------------------------------------------------------


async def _load(db: AsyncSession, media_session_id: str,
                owner_id: str | None) -> MediaSession:
    row = await db.get(MediaSession, media_session_id)
    if row is None or (row.owner_id is not None and row.owner_id != owner_id):
        raise MediaRuntimeError(f"media session {media_session_id!r} not found")
    return row


async def list_media_sessions(db: AsyncSession, owner_id: str | None, *,
                              kind: str | None = None, state: str | None = None,
                              limit: int = 50) -> list[dict]:
    if kind and kind not in KINDS:
        raise MediaRuntimeError(f"kind must be {'|'.join(KINDS)}, got {kind!r}")
    if state and state not in ("active", "ended"):
        raise MediaRuntimeError(f"state must be active|ended, got {state!r}")
    q = select(MediaSession).order_by(MediaSession.created_at.desc())
    if owner_id is not None:
        q = q.where(MediaSession.owner_id == owner_id)
    if kind:
        q = q.where(MediaSession.kind == kind)
    if state:
        q = q.where(MediaSession.state == state)
    rows = (await db.execute(q.limit(max(1, min(limit, MAX_LIST))))).scalars().all()
    return [_row_out(r) for r in rows]


def _row_out(row: MediaSession) -> dict:
    return {"id": row.id, "kind": row.kind, "title": row.title,
            "ref_kind": row.ref_kind, "ref_id": row.ref_id,
            "agent_id": row.agent_id, "state": row.state,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "ended_at": row.ended_at.isoformat() if row.ended_at else None}


# ---------------------------------------------------------------------------
# the unified projection
# ---------------------------------------------------------------------------


async def _call_events(db: AsyncSession, session_id: str) -> list[dict]:
    q = (select(VoiceEvent).where(VoiceEvent.session_id == session_id)
         .order_by(VoiceEvent.created_at.desc(), VoiceEvent.id.desc())
         .limit(MAX_VIEW_EVENTS))
    rows = list((await db.execute(q)).scalars().all())
    return [{"id": r.id, "kind": r.kind, "payload": r.payload or {},
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows]


async def _room_events(db: AsyncSession, legs: list[VoiceMeetingParticipant]) -> list[dict]:
    sids = [p.session_id for p in legs if p.session_id]
    if not sids:
        return []
    q = (select(VoiceEvent).where(VoiceEvent.session_id.in_(sids))
         .order_by(VoiceEvent.created_at.desc(), VoiceEvent.id.desc())
         .limit(MAX_VIEW_EVENTS))
    rows = list((await db.execute(q)).scalars().all())
    label_of = {p.session_id: (p.label or p.address or "participant")
                for p in legs if p.session_id}
    out = [{"id": r.id, "kind": r.kind, "payload": r.payload or {},
            "session_id": r.session_id, "participant": label_of.get(r.session_id),
            "created_at": r.created_at.isoformat() if r.created_at else None}
           for r in rows]
    out.sort(key=lambda e: e["created_at"] or "", reverse=True)
    return out[:MAX_VIEW_EVENTS]


def _call_tracks(session: VoiceSession) -> dict:
    """A call's audio track, derived from what actually flows through
    py8n: the media websocket's own stats when a stream is attached,
    honestly empty when the audio rides the carrier's media plane."""
    media = (session.context or {}).get("media") or {}
    audio = []
    if isinstance(media, dict) and media.get("stream_sid"):
        audio.append({"kind": "audio", "transport": "media_websocket",
                      "stream_sid": media.get("stream_sid"),
                      "chunks": media.get("chunks"), "audio_ms": media.get("audio_ms")})
    return {"audio": audio, "video": [], "screen": [],
            "note": ("py8n is not the media plane: a call's audio rides the "
                     "provider's stream through the media websocket when one "
                     "is attached; nothing is faked when it is not")}


async def media_view(db: AsyncSession, owner_id: str | None,
                     media_session_id: str) -> dict:
    """The MediaSession projection - one shape over calls and rooms."""
    row = await _load(db, media_session_id, owner_id)
    base = _row_out(row)
    base["modality"] = MODALITY_OF.get(row.kind, "audio")

    if row.ref_kind == "session":
        call = await db.get(VoiceSession, row.ref_id)
        if call is None:
            raise MediaRuntimeError(f"the wrapped call {row.ref_id!r} no longer exists")
        base["participants"] = [
            {"role": "caller", "address": call.from_ref, "label": call.from_ref,
             "state": call.state, "session_id": call.id, "channel": call.provider},
            {"role": "callee", "address": call.to_ref, "label": call.to_ref,
             "state": call.state, "session_id": None, "channel": call.provider},
        ]
        base["tracks"] = _call_tracks(call)
        base["data_channels"] = {
            "conversation": {"conversation_id": call.conversation_id,
                             "note": "the call's transcript lives in the linked interaction conversation"},
            "chat": None, "hand": None,
            "signaling": {"relay": None, "note": "signaling is a room concept"},
        }
        base["permissions"] = {
            "barge_in": bool((call.context or {}).get("barge_in", True)),
            "floor": floor_state_of_meeting_stub(),
            "mix": [],
        }
        base["presence"] = {"connected": call.state == "in_progress",
                            "state": call.state}
        base["events"] = await _call_events(db, call.id)
        base["recording"] = None
        base["transcription"] = {"transcript": "interaction conversation",
                                 "conversation_id": call.conversation_id}
        base["state"] = {"media": row.state, "call": call.state,
                         "end_reason": call.end_reason or None}
        return base

    room = await _load_meeting(db, row.ref_id, owner_id)
    legs = await _meeting_participants(db, room.id)
    label_of = {p.id: (p.label or p.address or "participant") for p in legs}
    vs = video_state(legs)
    video_tracks = [t for t in vs["tracks"] if t.get("kind") == "camera"]
    screen_tracks = [t for t in vs["tracks"] if t.get("kind") in ("screen", "screen_audio")]
    chat_rows = (await db.execute(
        select(VoiceMeetingMessage).where(VoiceMeetingMessage.meeting_id == room.id)
        .order_by(VoiceMeetingMessage.created_at.desc()).limit(1))).scalars().first()
    chat_count_q = select(VoiceMeetingMessage.id).where(VoiceMeetingMessage.meeting_id == room.id)
    chat_count = len((await db.execute(chat_count_q)).scalars().all())
    joined = [p for p in legs if p.state == "joined"]
    base["participants"] = [participant_out(p, await db.get(VoiceSession, p.session_id)
                                            if p.session_id else None) for p in legs]
    base["tracks"] = {
        "audio": [{"kind": "audio", "participant_id": p.id, "label": label_of.get(p.id),
                   "channel": p.channel, "session_id": p.session_id,
                   "state": p.state} for p in joined if p.session_id],
        "video": video_tracks, "screen": screen_tracks,
        "counts": vs["counts"],
        "note": vs["note"],
    }
    base["data_channels"] = {
        "chat": {"messages": chat_count,
                 "last_at": chat_rows.created_at.isoformat() if chat_rows else None},
        "hand": hand_queue_out(room, legs),
        "signaling": {"relay": "the media websockets (v78 relay)",
                      "note": "offers/answers/ICE are relayed live, never stored"},
    }
    base["permissions"] = {
        "mix": [{"participant_id": p.id, "label": label_of.get(p.id), **mix_of(p)}
                for p in legs],
        "floor": floor_state_of(room, legs),
        "note": "mix gates are enforced in the one turn funnel every channel shares",
    }
    base["presence"] = {
        "joined": len(joined),
        "dialing": sum(1 for p in legs if p.state in ("joining", "dialing")),
        "left": sum(1 for p in legs if p.state in ("left", "skipped", "failed")),
        "legs_with_live_session": sum(1 for p in legs
                                      if p.session_id and p.state == "joined"),
    }
    base["events"] = await _room_events(db, legs)
    from . import voice_recordings as rec_svc

    active = await rec_svc.active_recording_for_meeting(db, room.id)
    archives = await rec_svc.list_recordings(db, owner_id, meeting_id=room.id, limit=5)
    base["recording"] = active or None
    base["transcription"] = {
        "archives": [{"id": a["id"], "name": a["name"], "state": a["state"],
                      "artifacts": a.get("artifacts") or {}} for a in archives],
        "note": ("the room's words + the web legs' utterance audio, snapshotted "
                 "into artifacts at stop time"),
    }
    base["state"] = {"media": row.state, "room": room.state}
    return base


def floor_state_of_meeting_stub() -> dict:
    return {"mode": "auto", "note": "floor is a room concept"}


# ---------------------------------------------------------------------------
# mutations through the runtime
# ---------------------------------------------------------------------------


async def add_participant(db: AsyncSession, owner_id: str | None,
                          media_session_id: str, *, label: str,
                          channel: str = "web", address: str = "",
                          endpoint_id: str | None = None, sender=None) -> dict:
    """Add a leg through the runtime door (rooms only - a call's
    participants are the call, and the refusal says so)."""
    row = await _load(db, media_session_id, owner_id)
    if row.state != "active":
        raise MediaRuntimeError("the media session already ended")
    if row.ref_kind != "meeting":
        raise MediaRuntimeError(
            "kind 'voice' wraps a call - its participants are the call; "
            "open a video/meeting session to add legs")
    from . import voice_meetings as meetings_svc

    joined = await meetings_svc.join_participant(
        db, owner_id, row.ref_id, label=label, channel=channel,
        address=address, endpoint_id=endpoint_id, sender=sender)
    await emit(db, owner_id, "media.participant_added", source="media",
               actor=label, target_type="media_session", target_id=row.id,
               payload={"participant_id": joined["participant"]["id"],
                        "channel": channel, "meeting_id": row.ref_id},
               correlation_id=row.id)
    return {"participant": joined["participant"],
            "meeting": joined.get("meeting"),
            "media_session": await media_view(db, owner_id, row.id)}


async def end_media_session(db: AsyncSession, owner_id: str | None,
                            media_session_id: str) -> dict:
    """End the runtime object - and the primitive it wraps. A room ends
    through the meeting's own door (hang up the legs, stop the archive);
    a call hangs up through the state machine. Refusing to end twice is
    the honest answer."""
    row = await _load(db, media_session_id, owner_id)
    if row.state == "ended":
        raise MediaRuntimeError("the media session already ended")
    ended_under: dict
    if row.ref_kind == "session":
        from . import voice as voice_svc

        call = await db.get(VoiceSession, row.ref_id)
        if call is not None and call.state != "ended":
            await voice_svc.hangup(db, call, reason="media_session_ended")
        ended_under = {"call": call.state if call else "gone"}
    else:
        from . import voice_meetings as meetings_svc

        if (await _load_meeting(db, row.ref_id, owner_id)).state != "ended":
            ended_under = await meetings_svc.end_meeting(db, owner_id, row.ref_id)
        else:
            ended_under = {"room": "ended"}
    row.state = "ended"
    row.ended_at = _now()
    db.add(row)
    await db.flush()
    await emit(db, owner_id, "media.session_ended", source="media",
               actor=owner_id or "system", target_type="media_session",
               target_id=row.id,
               payload={"kind": row.kind, "ref_id": row.ref_id},
               correlation_id=row.id)
    return {"ended": True, "underlying": ended_under,
            "media_session_id": row.id}


__all__ = ["MediaRuntimeError", "KINDS", "MODALITY_OF", "REF_KIND_OF",
           "create_media_session", "media_view", "list_media_sessions",
           "add_participant", "end_media_session"]
