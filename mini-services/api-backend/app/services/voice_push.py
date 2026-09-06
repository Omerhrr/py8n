"""The push hub (v77) - the platform talks to a live leg while the stream runs.

v70 built the media websocket as a PIPE the provider (or the browser)
pushes audio through: the endpoint's loop blocked on ``receive_text``
and every server frame was a REPLY to something the client sent. But a
live leg is not only a listener - things happen on the platform side
that the leg must hear NOW:

* a chat message lands in the meeting the leg sits in (v76's room chat) -
  the web participant should see it the moment it is posted, not after a
  refresh;
* a queued caller's position changes (v76's channel queue) - the held leg
  should hear the new position, and (with a TTS engine bound) the actual
  audio of the announcement.

This module is the REGISTRY that makes those pushes possible: per voice
session, the set of media websockets currently connected in THIS
process. Pushing is a normal starlette send from another task - the hub
serializes sends per socket (two coroutines must never interleave
frames) and drops sockets that died mid-push. Everything is in-process
bookkeeping about LIVE CONNECTIONS: nothing is stored, nothing is
derived later - when the process restarts the registry is empty and
honest (a web leg reconnects and the hub learns it again).

Frames the platform pushes over the media websocket (server -> client):

* ``{"event": "chat", "meeting_id", "message": {...}}``      - room chat
* ``{"event": "queue_position", "queue_id", "queue_name",
     "position", "depth", "waited_seconds", "text"}``        - the line moved
* ``{"event": "audio", "audio_b64", "format": "wav",
     "source", "duration_ms"}``                              - spoken audio
  (the queue announcement's TTS, delivered to the held web leg)
* ``{"event": "video_track", "action": "published"|
     "unpublished", "meeting_id", "participant_id",
     "track_id", "kind"}``                                   - the room's
  video picture changed (v78 first-class video: the track registry)
* ``{"event": "video_signal", "meeting_id", "from",
     "from_label", "data"}``                                 - one WebRTC
  signaling frame (SDP offer/answer, ICE candidate) relayed from another
  leg of the meeting - py8n relays the handshake, the browsers carry the
  pixels
"""

from __future__ import annotations

import asyncio
import json

# frames the PLATFORM may push (server -> client) while the stream runs;
# the transport's own dialect (connected/start/media/mark/stop) stays the
# client's - a client pushing these events gets the honest unknown_event skip
PUSH_EVENTS = ("chat", "queue_position", "audio", "video_track", "video_signal")


class _Socket:
    """One connected media websocket, with its own send lock."""

    def __init__(self, websocket):
        self.websocket = websocket
        self.lock = asyncio.Lock()

    async def send(self, frame: dict) -> bool:
        try:
            async with self.lock:
                await self.websocket.send_text(json.dumps(frame, default=str))
            return True
        except Exception:  # noqa: BLE001 - the socket died mid-push
            return False


# session_id -> the sockets currently attached to that session's media stream
_registry: dict[str, set[_Socket]] = {}


def register(session_id: str, websocket) -> _Socket:
    """Attach one websocket to its session's push set (the media endpoint
    does this right after accept, and unregisters in its finally)."""
    sock = _Socket(websocket)
    _registry.setdefault(str(session_id), set()).add(sock)
    return sock


def unregister(session_id: str, sock: _Socket) -> None:
    bucket = _registry.get(str(session_id))
    if bucket is not None:
        bucket.discard(sock)
        if not bucket:
            _registry.pop(str(session_id), None)


def connection_count(session_id: str) -> int:
    """How many live media streams this process serves for the session."""
    return len(_registry.get(str(session_id), ()))


def connected_sessions() -> list[str]:
    """Session ids with at least one live media stream in this process."""
    return sorted(_registry)


async def push(session_id: str, frame: dict) -> int:
    """Push one frame to EVERY live media stream of the session.

    Returns the number of sockets the frame actually reached - the honest
    delivery count the caller records. Dead sockets are dropped from the
    registry on the way (they failed exactly once, visibly)."""
    bucket = _registry.get(str(session_id))
    if not bucket:
        return 0
    delivered = 0
    dead: list[_Socket] = []
    for sock in list(bucket):
        if await sock.send(frame):
            delivered += 1
        else:
            dead.append(sock)
    for sock in dead:
        unregister(session_id, sock)
    return delivered


async def push_to_session_legs(session_ids, frame: dict) -> dict:
    """Push to several legs at once (a meeting's participants): returns
    {legs: how many had a live socket, delivered: total frames accepted}."""
    legs = 0
    delivered = 0
    for sid in set(str(s) for s in session_ids if s):
        n = await push(sid, frame)
        if n:
            legs += 1
            delivered += n
    return {"legs": legs, "delivered": delivered}
