"""First-class video (v78) - the room gets a picture.

A zoom-like system needs the video to be a FIRST-CLASS modality of the
meeting, not a bolt-on. py8n's position is the same one that made the
audio side honest: py8n is NOT the media plane. The browser's WebRTC
stack carries the actual pixels peer-to-peer (or through the user's own
TURN server); py8n owns the SYSTEM layer around it:

* the TRACK REGISTRY - who is publishing what to the room (a camera
  track, a screen track, screen+audio), recorded on the participant's
  meta and on the leg's session timeline (video.started / video.stopped
  - traffic state about what the room was shown);
* the SIGNALING RELAY - WebRTC's offer/answer/ICE dance needs the peers
  to exchange SDP blobs and candidates. The room's web legs are already
  connected to py8n (the v70 media websocket); py8n relays the signaling
  frames between them through the v77 push hub. Ephemeral by nature:
  nothing about an offer or a candidate is stored - the handshake either
  reaches the peer's live socket (an honest delivered count) or it does
  not (an honest zero; the peer reconnects and the caller retries);
* the ROOM's VIDEO STATE - derived at read time: which legs have live
  camera/screen tracks, who holds the screen, the grid counts - the
  same derived-never-stored rule as the transcript and the floor.

So "build a zoom-like system" on py8n = meetings (the room, the legs,
the persona) + video (tracks + signaling) + chat (text side channel) +
hand-raise/floor (turn discipline) + queues (the waiting room) - every
piece a first-class primitive, composed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import VoiceMeetingParticipant, VoiceSession
from .voice_meetings import (VoiceMeetingError, _load as _load_meeting,
                             _participants as _meeting_participants)


class VideoError(ValueError):
    """Honest 4xx-grade video failures."""


TRACK_KINDS = ("camera", "screen", "screen_audio")
MAX_TRACKS_PER_LEG = 6  # a browser publishes a handful of streams, not a hundred


def _now():
    return datetime.now(timezone.utc)


def _live_tracks(p: VoiceMeetingParticipant) -> list[dict]:
    return [t for t in ((p.meta or {}).get("tracks") or [])
            if isinstance(t, dict) and not t.get("unpublished_at")]


def video_state(legs: list[VoiceMeetingParticipant]) -> dict:
    """The room's video picture, derived from the participants' meta at
    read time (nothing stored)."""
    tracks: list[dict] = []
    label_of = {p.id: (p.label or p.address or "participant") for p in legs}
    for p in legs:
        for t in _live_tracks(p):
            tracks.append({**t, "participant_id": p.id,
                           "label": label_of.get(p.id, "participant"),
                           "channel": p.channel})
    screen = [t for t in tracks if t.get("kind") in ("screen", "screen_audio")]
    return {
        "tracks": tracks,
        "counts": {"live_tracks": len(tracks),
                   "camera": sum(1 for t in tracks if t.get("kind") == "camera"),
                   "screen": len(screen),
                   "legs_with_video": len({t["participant_id"] for t in tracks})},
        "screen_holders": sorted({t["label"] for t in screen}),
        "note": ("py8n is not the video mixer: the browsers' WebRTC stacks carry "
                 "the pixels (peer-to-peer or the owner's TURN server); py8n owns "
                 "the track registry, the signaling relay over the media "
                 "websockets and this derived state"),
    }


async def publish_track(db: AsyncSession, owner_id: str | None, meeting_id: str,
                        participant_id: str, *, track_id: str, kind: str,
                        label: str = "") -> dict:
    """A leg starts showing the room a track (camera or screen). The
    registration is recorded on the participant's meta, a video.started
    event lands on the leg's session timeline, and every OTHER live web
    leg is pushed the news (the v77 push hub) so its UI can render the
    newcomer and its WebRTC stack can expect the offer."""
    meeting = await _load_meeting(db, meeting_id, owner_id)
    if meeting.state != "active":
        raise VideoError("the meeting already ended - tracks publish in active rooms")
    p = await db.get(VoiceMeetingParticipant, participant_id)
    if p is None or p.meeting_id != meeting.id:
        raise VideoError(f"participant {participant_id!r} not found in meeting {meeting_id!r}")
    if p.state != "joined":
        raise VideoError(f"only joined legs publish tracks, got {p.state!r}")
    track_id = (track_id or "").strip()[:140]
    if not track_id:
        raise VideoError("a track_id is required (the publisher's own stream id)")
    if kind not in TRACK_KINDS:
        raise VideoError(f"track kind must be {'|'.join(TRACK_KINDS)}, got {kind!r}")
    live = _live_tracks(p)
    if any(t.get("track_id") == track_id for t in live):
        raise VideoError(f"track {track_id!r} is already live - unpublish it first")
    if len(live) >= MAX_TRACKS_PER_LEG:
        raise VideoError(f"this leg already publishes {MAX_TRACKS_PER_LEG} live tracks")
    meta = dict(p.meta or {})
    # the same copy discipline as unpublish: fresh dicts only (a mutation
    # of a shared inner dict would make old == new and silently lose the
    # write), then a FRESH track dict is appended
    tracks = [dict(t) for t in (meta.get("tracks") or [])
              if isinstance(t, dict)]
    tracks.append({"track_id": track_id, "kind": kind,
                   "label": (label or "").strip()[:140],
                   "published_at": _now().isoformat()})
    meta["tracks"] = tracks
    p.meta = meta
    db.add(p)
    await db.flush()
    # the leg's session timeline keeps the record (video.started)
    event = None
    if p.session_id:
        session = await db.get(VoiceSession, p.session_id)
        if session is not None:
            from . import voice as voice_svc

            event = await voice_svc._add_event(
                db, session, "video.started",
                {"track_id": track_id, "kind": kind, "label": (label or "").strip()[:140],
                 "meeting_id": meeting.id, "participant_id": p.id})
    # tell the OTHER legs (the publisher's own UI already knows)
    from . import voice_push

    others = [q for q in await _meeting_participants(db, meeting.id)
              if q.id != p.id and q.session_id]
    frame = {"event": "video_track", "action": "published", "meeting_id": meeting.id,
             "participant_id": p.id, "label": p.label or p.address,
             "track_id": track_id, "kind": kind}
    push = await voice_push.push_to_session_legs([q.session_id for q in others], frame)
    from . import system_events as events_svc

    await events_svc.emit(db, owner_id, "track.published", source="video",
                          actor=p.label or p.address or "participant",
                          target_type="meeting", target_id=meeting.id,
                          payload={"track_id": track_id, "kind": kind,
                                   "participant_id": p.id},
                          correlation_id=meeting.id, session_id=p.session_id)
    return {"meeting_id": meeting.id, "participant_id": p.id,
            "track": {**tracks[-1]}, "event_id": event.id if event else None,
            "push": {**push, "notified_legs": len(others)},
            "video": video_state(await _meeting_participants(db, meeting.id))}


async def unpublish_track(db: AsyncSession, owner_id: str | None, meeting_id: str,
                          participant_id: str, *, track_id: str) -> dict:
    """A leg stops showing a track (camera off, screen share ended). The
    meta entry is stamped unpublished_at (history kept - the room's past
    is traffic too), video.stopped lands on the timeline, the other legs
    are pushed the news."""
    meeting = await _load_meeting(db, meeting_id, owner_id)
    p = await db.get(VoiceMeetingParticipant, participant_id)
    if p is None or p.meeting_id != meeting.id:
        raise VideoError(f"participant {participant_id!r} not found in meeting {meeting_id!r}")
    meta = dict(p.meta or {})
    # COPY the track dicts before mutating - the loaded JSON column's
    # inner dicts are shared objects, and mutating one in place changes
    # the OLD attribute value too (old == new means the ORM silently
    # skips the UPDATE - the exact trap publish avoids with fresh dicts)
    tracks = [dict(t) for t in (meta.get("tracks") or [])
              if isinstance(t, dict)]
    hit = next((t for t in tracks
                if t.get("track_id") == track_id and not t.get("unpublished_at")), None)
    if hit is None:
        raise VideoError(f"track {track_id!r} is not live on this leg")
    hit["unpublished_at"] = _now().isoformat()
    meta["tracks"] = tracks
    p.meta = meta
    db.add(p)
    await db.flush()
    event = None
    if p.session_id:
        session = await db.get(VoiceSession, p.session_id)
        if session is not None:
            from . import voice as voice_svc

            event = await voice_svc._add_event(
                db, session, "video.stopped",
                {"track_id": track_id, "kind": hit.get("kind"),
                 "meeting_id": meeting.id, "participant_id": p.id})
    from . import voice_push

    others = [q for q in await _meeting_participants(db, meeting.id)
              if q.id != p.id and q.session_id]
    frame = {"event": "video_track", "action": "unpublished", "meeting_id": meeting.id,
             "participant_id": p.id, "label": p.label or p.address,
             "track_id": track_id, "kind": hit.get("kind")}
    push = await voice_push.push_to_session_legs([q.session_id for q in others], frame)
    from . import system_events as events_svc

    await events_svc.emit(db, owner_id, "track.unpublished", source="video",
                          actor=p.label or p.address or "participant",
                          target_type="meeting", target_id=meeting.id,
                          payload={"track_id": track_id, "kind": hit.get("kind"),
                                   "participant_id": p.id},
                          correlation_id=meeting.id, session_id=p.session_id)
    return {"meeting_id": meeting.id, "participant_id": p.id,
            "track": hit, "event_id": event.id if event else None,
            "push": {**push, "notified_legs": len(others)},
            "video": video_state(await _meeting_participants(db, meeting.id))}


async def relay_signal(db: AsyncSession, owner_id: str | None, meeting_id: str,
                       *, from_participant_id: str, to_participant_id: str,
                       data: dict) -> dict:
    """Relay one WebRTC signaling frame between two legs of the room.

    ``data`` is the publisher's payload (an SDP offer/answer, an ICE
    candidate, whatever the browsers negotiate) - py8n does not parse
    it, does not store it, does not pretend to understand it. It finds
    the target leg's session and pushes the frame through the v77 push
    hub; the honest delivery count is the whole report."""
    meeting = await _load_meeting(db, meeting_id, owner_id)
    if meeting.state != "active":
        raise VideoError("the meeting already ended - signaling belongs to active rooms")
    legs = {p.id: p for p in await _meeting_participants(db, meeting.id)}
    src = legs.get(from_participant_id)
    dst = legs.get(to_participant_id)
    if src is None or dst is None:
        raise VideoError("both legs must be participants of this meeting")
    if src.state != "joined" or dst.state != "joined":
        raise VideoError(
            f"signaling runs between JOINED legs (from={src.state}, to={dst.state})")
    if not isinstance(data, dict):
        raise VideoError("data must be the signaling payload (a JSON object)")
    if not dst.session_id:
        return {"delivered": 0, "note": "the target leg has no session to signal"}
    from . import voice_push

    n = await voice_push.push(dst.session_id, {
        "event": "video_signal", "meeting_id": meeting.id,
        "from": src.id, "from_label": src.label or src.address,
        "data": data})
    return {"delivered": n,
            "note": ("delivered = the frames that reached the target leg's live "
                     "media websocket; 0 means the peer is not connected right "
                     "now - retry when it is (signaling is ephemeral, never "
                     "queued or stored)")}


async def relay_from_session(db: AsyncSession, session_id: str, *,
                             to_participant_id: str, data: dict) -> dict:
    """The media-websocket entry point: a frame arriving on a leg's
    socket is routed by the SESSION (the socket does not know its
    participant id). Refuses honestly when the session is nobody's leg."""
    q = (select(VoiceMeetingParticipant)
         .where(VoiceMeetingParticipant.session_id == session_id,
                VoiceMeetingParticipant.state == "joined")
         .order_by(VoiceMeetingParticipant.created_at.desc()))
    src = (await db.execute(q)).scalars().first()
    if src is None:
        raise VideoError("this session is not a joined leg of any meeting")
    return await relay_signal(db, src.owner_id, src.meeting_id,
                              from_participant_id=src.id,
                              to_participant_id=to_participant_id,
                              data=data)
