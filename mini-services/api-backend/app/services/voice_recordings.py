"""Recording / transcription archives for meetings (v79) - what py8n
actually carries, archived honestly.

A zoom-like system records its meetings. py8n's position on that is the
same one that made the video honest: py8n is NOT the media plane, so it
does not pretend to hold pixels it never saw. What py8n DOES carry, and
therefore can archive, is:

* the room's WORDS - the derived merged transcript (every leg's
  asr.final and tts.started, speaker-attributed) and the group-chat log,
  snapshotted into artifacts when the recording stops. The transcription
  archive is a snapshot of exactly what the room's timelines say, frozen
  at stop time;
* the AUDIO of the web legs - the media websocket (v70) already decodes
  each utterance's linear16 PCM to run VAD + ASR. While a recording is
  active, those utterance buffers accumulate in an in-process capture
  registry (the same in-process honesty as the push hub: when the
  process dies, the un-flushed audio is gone and the archive says so).
  At stop time each captured leg's audio is written as a REAL WAV
  artifact (RIFF header, mono, the stream's own sample rate). Phone
  legs ride the carrier's media plane - py8n never has their audio and
  the archive's per-leg coverage block says exactly that.

A recording is a first-class row (VoiceMeetingRecording) so the archive
has a HANDLE; the content lives in regular artifacts. Lifecycle:

    POST /meetings/{id}/recordings         -> state=recording, capture
                                              starts for the joined web
                                              legs, recording.started on
                                              every leg's timeline
    (legs joining a recorded room start capturing automatically)
    POST .../recordings/{rid}/stop         -> transcript + chat + WAV
                                              artifacts written, meta
                                              stamped, state=stopped
    POST /meetings/{id}/end                -> an active recording is
                                              stopped automatically (the
                                              room cannot outlive its
                                              archive)
"""

from __future__ import annotations

import io
import json
import wave
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (Artifact, VoiceMeetingParticipant, VoiceMeetingRecording,
                      VoiceSession)
from .voice_meetings import (VoiceMeetingError, _load as _load_meeting,
                             _participants as _meeting_participants,
                             merged_transcript)

# the capture registry's honest ceilings (a recording is minutes of
# speech, not hours of raw PCM in RAM)
MAX_SESSIONS_PER_RECORDING = 64
MAX_PCM_BYTES_PER_SESSION = 64 * 1024 * 1024  # ~32 minutes of 16k linear16


class RecordingError(ValueError):
    """Honest 4xx-grade recording failures."""


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# the in-process utterance capture (per session of a recorded room)
# ---------------------------------------------------------------------------

# session_id -> {"recording_id", "sample_rate", "chunks": [(pcm, ms)],
#                "utterances", "pcm_bytes", "skipped", "started_at"}
_captures: dict[str, dict] = {}


def start_capture(session_id: str, recording_id: str) -> bool:
    """Begin capturing a web leg's utterances for a recording. In-process
    by contract: live sockets are process state, and so is the audio they
    are streaming right now."""
    sid = str(session_id)
    if sid in _captures or len(_captures) >= MAX_SESSIONS_PER_RECORDING:
        return False
    _captures[sid] = {"recording_id": str(recording_id), "sample_rate": None,
                      "chunks": [], "utterances": 0, "pcm_bytes": 0,
                      "skipped": 0, "started_at": _now().isoformat()}
    return True


def append_utterance(session_id: str, pcm: bytes, duration_ms: float,
                     sample_rate: int | None = None) -> bool:
    """One closed utterance (the media loop's speech.ended segment) into
    the session's capture. Refuses honestly when this leg is not being
    recorded; skips silently-counted when the capture is over its honest
    ceiling. Returns whether the audio was kept."""
    cap = _captures.get(str(session_id))
    if cap is None or not pcm:
        return False
    rate = int(sample_rate or 0) or None
    if cap["sample_rate"] is None:
        cap["sample_rate"] = rate
    elif rate is not None and rate != cap["sample_rate"]:
        cap["skipped"] += 1  # a mid-stream rate change would corrupt the WAV
        return False
    if cap["pcm_bytes"] + len(pcm) > MAX_PCM_BYTES_PER_SESSION:
        cap["skipped"] += 1
        return False
    cap["chunks"].append((pcm, float(duration_ms or 0.0)))
    cap["utterances"] += 1
    cap["pcm_bytes"] += len(pcm)
    return True


def capture_snapshot(session_id: str) -> dict | None:
    cap = _captures.get(str(session_id))
    if cap is None:
        return None
    return {"recording_id": cap["recording_id"], "utterances": cap["utterances"],
            "pcm_bytes": cap["pcm_bytes"], "skipped": cap["skipped"],
            "sample_rate": cap["sample_rate"]}


def pop_capture(session_id: str) -> dict | None:
    """Finish one leg's capture: the buffer leaves the registry (stop
    time); the WAV writer takes it from here."""
    return _captures.pop(str(session_id), None)


def stop_captures_for_recording(recording_id: str) -> list[str]:
    """Drop every capture bound to a recording (e.g. a failed stop) -
    returns the session ids that were captured."""
    sids = [sid for sid, cap in _captures.items()
            if cap.get("recording_id") == str(recording_id)]
    for sid in sids:
        _captures.pop(sid, None)
    return sids


def capture_stats() -> dict:
    """Registry-wide honesty (the diagnostics view): what is captured now."""
    return {"sessions": len(_captures),
            "recordings": len({cap.get("recording_id") for cap in _captures.values()}),
            "pcm_bytes": sum(cap["pcm_bytes"] for cap in _captures.values())}


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """linear16 mono PCM -> a real RIFF/WAVE file (stdlib wave)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# the recording lifecycle
# ---------------------------------------------------------------------------

async def _active_for_meeting(db: AsyncSession, meeting_id: str) -> VoiceMeetingRecording | None:
    q = (select(VoiceMeetingRecording)
         .where(VoiceMeetingRecording.meeting_id == meeting_id,
                VoiceMeetingRecording.state == "recording")
         .order_by(VoiceMeetingRecording.started_at.desc()))
    return (await db.execute(q)).scalars().first()


async def active_recording_for_meeting(db: AsyncSession, meeting_id: str) -> dict | None:
    row = await _active_for_meeting(db, meeting_id)
    return (await recording_out(db, row)) if row is not None else None


async def recording_out(db: AsyncSession, row: VoiceMeetingRecording,
                        *, include: bool = False) -> dict:
    """The recording as the API returns it: the row + DERIVED room facts
    (participant labels, per-leg coverage). ``include`` embeds the
    archived transcript/chat lines read back from the artifacts."""
    legs = await _meeting_participants(db, row.meeting_id)
    label_of = {p.session_id: (p.label or p.address or "participant")
                for p in legs if p.session_id}
    channel_of = {p.session_id: p.channel for p in legs if p.session_id}
    meta = row.meta or {}
    audio_blocks = []
    for a in (meta.get("audio") or []):
        audio_blocks.append({**a, "label": label_of.get(a.get("session_id"),
                                                      a.get("session_id"))})
    out = {
        "id": row.id, "meeting_id": row.meeting_id, "name": row.name,
        "state": row.state,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "meta": meta,
        "counts": {
            "transcript_lines": int(meta.get("transcript_lines") or 0),
            "chat_lines": int(meta.get("chat_lines") or 0),
            "audio_legs": len(audio_blocks),
            "legs_in_room": len(legs),
        },
        "artifacts": {
            "transcript": meta.get("transcript_artifact_id"),
            "chat": meta.get("chat_artifact_id"),
            "audio": audio_blocks,
        },
        "participants": [{"session_id": p.session_id,
                          "label": p.label or p.address or "participant",
                          "channel": p.channel,
                          "captured": bool(p.session_id
                                           and (meta.get("audio") or [])
                                           and any(a.get("session_id") == p.session_id
                                                   for a in meta.get("audio") or []))}
                         for p in legs],
        "note": ("py8n archives what it CARRIES: the words (transcript + chat "
                 "snapshots) and the web legs' utterance audio (media websocket). "
                 "Phone legs ride the carrier's media plane - their audio is "
                 "honestly absent; the pixels of a video meeting ride the "
                 "browsers' WebRTC stacks and are never held by py8n at all"),
    }
    if include:
        for key, art_id, kind in (("transcript", meta.get("transcript_artifact_id"), "text"),
                                  ("chat", meta.get("chat_artifact_id"), "json")):
            if not art_id:
                continue
            art = await db.get(Artifact, art_id)
            if art is None:
                continue
            try:
                raw = artifacts_bytes(art)
                # the transcript is markdown (a faithful text snapshot), the
                # chat log is json - each is read back as itself
                out[key] = raw.decode("utf-8") if kind == "text" else json.loads(raw)
            except Exception:  # noqa: BLE001 - a broken snapshot is
                out[key] = None  # reported, never fatal
    return out


def artifacts_bytes(row: Artifact) -> bytes:
    from .artifacts import read_bytes

    return read_bytes(row)


async def start_recording(db: AsyncSession, owner_id: str | None, meeting_id: str,
                          *, name: str = "") -> dict:
    """Open a recording pass over an ACTIVE room: one per meeting (a room
    records once at a time), capture starts for the joined web legs,
    every leg's timeline learns the room is being recorded."""
    meeting = await _load_meeting(db, meeting_id, owner_id)
    if meeting.state != "active":
        raise RecordingError("the meeting already ended - recordings cover live rooms")
    active = await _active_for_meeting(db, meeting.id)
    if active is not None:
        raise RecordingError(
            f"this meeting already has a recording in progress ({active.id}) - "
            "stop it before starting another")
    row = VoiceMeetingRecording(meeting_id=meeting.id, owner_id=owner_id,
                                name=(name or "").strip()[:200]
                                or f"recording {meeting.title or meeting.id}"[:200],
                                state="recording", meta={})
    db.add(row)
    await db.flush()
    # capture starts for the legs ALREADY streaming (later joins hook in
    # through join_participant)
    legs = await _meeting_participants(db, meeting.id)
    capture_started: list[str] = []
    for p in legs:
        if p.channel == "web" and p.session_id:
            if start_capture(p.session_id, row.id):
                capture_started.append(p.session_id)
    # the room is told (traffic state on the legs' own timelines)
    started_ids = []
    for p in legs:
        if p.session_id:
            session = await db.get(VoiceSession, p.session_id)
            if session is not None and session.state != "ended":
                ev = await _voice_event(db, session, "recording.started",
                                        {"recording_id": row.id, "name": row.name,
                                         "meeting_id": meeting.id})
                started_ids.append(ev.id)
    await db.flush()
    out = await recording_out(db, row)
    out.update({"capture_started_sessions": capture_started,
                "events": started_ids,
                "note": ("recording - the transcript and chat snapshot at stop "
                         "time; web legs' utterance audio accumulates from NOW "
                         "(earlier speech predates the recording)")})
    return out


async def _voice_event(db: AsyncSession, session: VoiceSession, kind: str,
                       payload: dict):
    from . import voice as voice_svc

    return await voice_svc._add_event(db, session, kind, payload)


def _render_transcript_markdown(recording: VoiceMeetingRecording, meeting,
                                lines: list[dict], chat: list[dict],
                                legs: list[VoiceMeetingParticipant]) -> str:
    """The transcription archive's human format - a faithful snapshot,
    not a reinterpretation."""
    started = recording.started_at.isoformat() if recording.started_at else "?"
    people = ", ".join(
        f"{p.label or p.address or 'participant'} [{p.channel}]" for p in legs) or "none"
    parts = [f"# {recording.name}",
             "",
             f"- meeting: {meeting.title or meeting.id} ({meeting.id})",
             f"- recorded: {started} -> {(_now().isoformat())}",
             f"- participants: {people}",
             f"- agent: {getattr(meeting, 'agent_id', None) or 'none'}",
             "",
             "## Transcript",
             ""]
    if lines:
        for ln in lines:
            at = (ln.get("at") or "").replace("T", " ")[:19]
            who = ln.get("speaker") or "?"
            text = str(ln.get("text") or "").strip()
            conf = ln.get("confidence")
            tag = f" (conf {conf:.2f})" if isinstance(conf, (int, float)) else ""
            parts.append(f"- **{who}**{tag} ({at}): {text}")
    else:
        parts.append("_(the room's legs produced no transcribed speech while "
                     "this recording ran)_")
    parts += ["", "## Chat", ""]
    if chat:
        for m in chat:
            at = (str(m.get("created_at") or "")).replace("T", " ")[:19]
            parts.append(f"- **{m.get('author') or '?'}** [{m.get('role') or 'member'}]"
                         f" ({at}): {m.get('text') or ''}")
    else:
        parts.append("_(no chat messages during this recording)_")
    return "\n".join(parts) + "\n"


async def stop_recording(db: AsyncSession, owner_id: str | None, meeting_id: str,
                         recording_id: str, *,
                         reason: str = "stopped") -> dict:
    """Close the pass and write the ARCHIVE: the transcript + chat
    snapshots (artifacts) and every captured web leg's utterance audio
    (real WAV files). The room's legs learn the recording stopped."""
    meeting = await _load_meeting(db, meeting_id, owner_id)
    row = await db.get(VoiceMeetingRecording, recording_id)
    if row is None or row.meeting_id != meeting.id:
        raise RecordingError(f"recording {recording_id!r} not found in meeting "
                             f"{meeting_id!r}")
    if row.state != "recording":
        raise RecordingError(f"the recording is already {row.state}")
    from .artifacts import save_artifact

    legs = await _meeting_participants(db, meeting.id)
    session_ids = [p.session_id for p in legs if p.session_id]
    sessions: dict[str, VoiceSession] = {}
    if session_ids:
        q = select(VoiceSession).where(VoiceSession.id.in_(session_ids))
        sessions = {s.id: s for s in (await db.execute(q)).scalars().all()}

    # 1. the words: the merged transcript + chat, frozen now
    lines = await merged_transcript(db, legs, sessions)
    try:
        chat = await _chat_log(db, meeting.id)
    except VoiceMeetingError:
        chat = []
    transcript_md = _render_transcript_markdown(row, meeting, lines, chat, legs)
    transcript_art = await save_artifact(
        db, kind="recording", data=transcript_md.encode("utf-8"),
        content_type="text/markdown",
        meta={"recording_id": row.id, "meeting_id": meeting.id,
              "what": "meeting_transcript", "lines": len(lines)},
        filename=f"meeting-{meeting.id}-transcript.md")
    chat_art = await save_artifact(
        db, kind="recording", data=json.dumps({"recording_id": row.id,
                                               "meeting_id": meeting.id,
                                               "messages": chat},
                                              default=str).encode("utf-8"),
        content_type="application/json",
        meta={"recording_id": row.id, "meeting_id": meeting.id,
              "what": "meeting_chat", "lines": len(chat)},
        filename=f"meeting-{meeting.id}-chat.json")

    # 2. the audio: every captured web leg's utterances -> one WAV each
    audio_blocks: list[dict] = []
    for p in legs:
        if not p.session_id:
            continue
        cap = pop_capture(p.session_id)
        if cap is None:
            audio_blocks.append({"session_id": p.session_id,
                                 "captured": False,
                                 "detail": ("no audio flowed through py8n for "
                                            "this leg while the recording ran"
                                            if p.channel == "web" else
                                            "phone leg - the audio rides the "
                                            "carrier's media plane, py8n never "
                                            "holds it")})
            continue
        pcm = b"".join(chunk for chunk, _ms in cap["chunks"])
        if not pcm:
            audio_blocks.append({"session_id": p.session_id, "captured": False,
                                 "detail": "the capture held no utterances"})
            continue
        wav = pcm_to_wav(pcm, cap.get("sample_rate") or 16000)
        art = await save_artifact(
            db, kind="recording", data=wav, content_type="audio/wav",
            meta={"recording_id": row.id, "meeting_id": meeting.id,
                  "what": "leg_audio", "session_id": p.session_id,
                  "utterances": cap["utterances"],
                  "skipped_frames": cap["skipped"]},
            filename=f"meeting-{meeting.id}-leg-{p.session_id[:8]}.wav")
        total_ms = round(sum(ms for _chunk, ms in cap["chunks"]), 1)
        audio_blocks.append({"session_id": p.session_id, "captured": True,
                             "artifact_id": art.id,
                             "utterances": cap["utterances"],
                             "duration_ms": total_ms,
                             "sample_rate": cap.get("sample_rate")})

    meta = dict(row.meta or {})
    meta.update({"transcript_artifact_id": transcript_art.id,
                 "chat_artifact_id": chat_art.id,
                 "transcript_lines": len(lines), "chat_lines": len(chat),
                 "audio": audio_blocks, "stop_reason": reason})
    row.meta = meta
    row.state = "stopped"
    row.ended_at = _now()
    db.add(row)
    # the room is told
    for p in legs:
        if p.session_id:
            session = sessions.get(p.session_id)
            if session is not None and session.state != "ended":
                await _voice_event(db, session, "recording.stopped",
                                   {"recording_id": row.id, "reason": reason,
                                    "transcript_artifact_id": transcript_art.id})
    await db.flush()
    out = await recording_out(db, row)
    out["note"] = ("the archive is written: transcript + chat snapshots and the "
                   "captured web legs' utterance audio (WAV), each a regular "
                   "artifact - the recording row is the handle")
    return out


async def _chat_log(db: AsyncSession, meeting_id: str) -> list[dict]:
    from .voice_meetings import get_chat

    return await get_chat(db, meeting_id, None, limit=500)


async def list_recordings(db: AsyncSession, owner_id: str | None,
                          meeting_id: str | None = None,
                          limit: int = 50) -> list[dict]:
    q = select(VoiceMeetingRecording).order_by(
        VoiceMeetingRecording.started_at.desc())
    if meeting_id:
        q = q.where(VoiceMeetingRecording.meeting_id == meeting_id)
    if owner_id is not None:
        q = q.where(VoiceMeetingRecording.owner_id == owner_id)
    q = q.limit(max(1, min(limit, 200)))
    rows = list((await db.execute(q)).scalars().all())
    return [await recording_out(db, r) for r in rows]


async def get_recording(db: AsyncSession, owner_id: str | None,
                        recording_id: str, *, include: bool = False) -> dict:
    row = await db.get(VoiceMeetingRecording, recording_id)
    if row is None or (owner_id is not None and row.owner_id is not None
                       and row.owner_id != owner_id):
        raise RecordingError(f"recording {recording_id!r} not found")
    return await recording_out(db, row, include=include)


async def stop_active_for_meeting(db: AsyncSession, meeting,
                                  *, reason: str) -> dict | None:
    """The auto-close: a meeting that ends stops its active recording (a
    room cannot outlive its archive). Best-effort by contract - a failed
    archive lands on the row as state=failed, never on the room's end."""
    row = await _active_for_meeting(db, meeting.id)
    if row is None:
        return None
    try:
        return await stop_recording(db, meeting.owner_id, meeting.id, row.id,
                                    reason=reason)
    except Exception:  # noqa: BLE001 - the room's end must not break
        row.state = "failed"
        row.ended_at = _now()
        row.meta = {**(row.meta or {}), "stop_reason": f"failed: {reason}"}
        db.add(row)
        await db.flush()
        return None
