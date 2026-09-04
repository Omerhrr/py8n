"""Voice audio transport (v70) - media streams and websocket ASR.

v69 made the call a first-class object (state machine, barge-in, ASR/TTS
contract) but turns arrived as finished HTTP posts. Real voice agents
consume MEDIA STREAMS: the provider opens a websocket to py8n and pushes
the call's audio in 20ms chunks (Twilio <Connect><Stream>, Telnyx
streaming bidirectional, Deepgram-style JSON envelopes - the dialect is
de-facto standardized). This module is the transport, PURE:

* **media frames** - ``parse_media_frame`` normalizes the JSON envelope:
  ``connected | start | media | mark | stop`` with base64 audio payloads,
  honest skips for anything else (keepalive pings, unknown events).
* **audio decode** - G.711 u-law (the 8kHz telephony codec every major
  provider streams) decoded to linear16 PCM with the standard CCITT
  algorithm (Sun g711 reference formula, byte-exact).
* **VAD + segmentation** - RMS voice-activity detection over the decoded
  chunks; an ``UtteranceSegmenter`` turns the continuous audio flow into
  UTTERANCES (speech starts, silence closes them) carrying the exact PCM
  of the utterance - the unit a streaming ASR engine transcribes.
* **ASR engine registry** - the websocket-ASR contract: an engine is any
  callable ``(pcm_linear16, sample_rate) -> asr result dict`` validated
  through the v69 ``validate_asr_result`` contract. Real engines
  (deepgram live, whisper.cpp, vosk) register by name; with NO engine
  bound the transport honestly reports ``asr.unavailable`` instead of
  pretending it heard something.

Nothing here touches the database - the websocket endpoint in
api/voice.py owns sessions, events and turns; this module owns the
audio semantics so every rule is unit-testable without a socket.
"""

from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------------------
# G.711 u-law -> linear16 (CCITT G.711, Sun g711 reference formula)
# ---------------------------------------------------------------------------

_BIAS = 0x84


def mulaw_to_linear(sample: int) -> int:
    """One u-law byte -> one linear16 sample (14-bit signed range)."""
    u = ~sample & 0xFF
    exponent = (u & 0x70) >> 4
    mantissa = u & 0x0F
    t = ((mantissa << 3) + _BIAS) << exponent
    return (_BIAS - t) if (u & 0x80) else (t - _BIAS)


def mulaw_to_linear16(data: bytes) -> bytes:
    """Decode a u-law buffer to little-endian int16 PCM."""
    return struct.pack(f"<{len(data)}h", *(mulaw_to_linear(b) for b in data))


AUDIO_ENCODINGS = ("mulaw", "linear16")  # what media payloads may carry

MEDIA_SAMPLE_RATE = 8000  # telephony default; linear16 payloads may override


# ---------------------------------------------------------------------------
# Media stream frames - the JSON envelope providers push
# ---------------------------------------------------------------------------


@dataclass
class MediaFrame:
    """One normalized websocket frame from a media stream."""

    event: str                       # connected|start|media|mark|stop
    stream_sid: str = ""             # provider stream id (from start)
    call_ref: str = ""               # provider call id (from start)
    payload_b64: str = ""            # media payload (base64 audio)
    encoding: str = "mulaw"          # mulaw | linear16
    sample_rate: int = MEDIA_SAMPLE_RATE
    track: str = ""                  # inbound|outbound|both
    chunk_ms: float = 20.0           # audio duration of one chunk
    mark_name: str = ""              # mark event name
    custom_parameters: dict = field(default_factory=dict)
    sequence: int = 0


MEDIA_EVENTS = ("connected", "start", "media", "mark", "stop")


def parse_media_frame(frame: dict, *, default_encoding: str = "mulaw") -> tuple[MediaFrame | None, dict | None]:
    """Normalize one provider frame -> (MediaFrame, skip_reason_or_None).

    Skips are honest and non-fatal: keepalives, unknown events, media
    chunks with undecodable base64. A frame that is not an object is a
    hard error (the caller closes the socket with a reason).
    """
    if not isinstance(frame, dict):
        return None, {"reason": "not_an_object", "detail": "media frames must be JSON objects"}
    event = str(frame.get("event") or "").strip().lower()
    if event not in MEDIA_EVENTS:
        return None, {"reason": "unknown_event", "detail": f"event {event!r} is not part of the media dialect"}
    out = MediaFrame(event=event)
    if event == "start":
        start = frame.get("start") or {}
        out.stream_sid = str(start.get("streamSid") or start.get("stream_sid") or "")
        out.call_ref = str(start.get("callSid") or start.get("call_sid") or "")
        out.custom_parameters = dict(start.get("customParameters") or start.get("custom_parameters") or {})
        out.encoding = str(out.custom_parameters.get("encoding") or default_encoding).lower()
        if out.encoding not in AUDIO_ENCODINGS:
            return None, {"reason": "unsupported_encoding",
                          "detail": f"custom parameter encoding {out.encoding!r} unsupported "
                                    f"(known: {', '.join(AUDIO_ENCODINGS)})"}
        try:
            out.sample_rate = int(out.custom_parameters.get("sample_rate") or MEDIA_SAMPLE_RATE)
        except (TypeError, ValueError):
            out.sample_rate = MEDIA_SAMPLE_RATE
    elif event == "media":
        media = frame.get("media") or {}
        out.payload_b64 = str(media.get("payload") or "")
        out.track = str(media.get("track") or "")
        try:
            out.sequence = int(media.get("chunk") or media.get("sequence") or 0)
        except (TypeError, ValueError):
            out.sequence = 0
        out.encoding = str(media.get("encoding") or default_encoding).lower()
        if out.encoding not in AUDIO_ENCODINGS:
            return None, {"reason": "unsupported_encoding",
                          "detail": f"encoding {out.encoding!r} unsupported (known: {', '.join(AUDIO_ENCODINGS)})"}
        try:
            out.sample_rate = int(media.get("sample_rate") or MEDIA_SAMPLE_RATE)
        except (TypeError, ValueError):
            out.sample_rate = MEDIA_SAMPLE_RATE
    elif event == "mark":
        mark = frame.get("mark") or {}
        out.mark_name = str(mark.get("name") or mark.get("mark") or "")
    return out, None


def decode_audio_chunk(frame: MediaFrame) -> tuple[bytes, float, dict | None]:
    """One media frame -> (linear16 pcm, duration_ms, skip_or_None)."""
    try:
        raw = base64.b64decode(frame.payload_b64, validate=True)
    except (binascii.Error, ValueError):
        return b"", 0.0, {"reason": "bad_base64", "detail": "media payload is not valid base64"}
    if not raw:
        return b"", 0.0, {"reason": "empty_payload", "detail": "media payload carried no audio"}
    if frame.encoding == "linear16":
        pcm = raw
        if len(pcm) % 2:
            pcm = pcm[: len(pcm) // 2 * 2]  # drop a trailing odd byte honestly
        samples = len(pcm) // 2
    else:
        pcm = mulaw_to_linear16(raw)
        samples = len(raw)
    rate = max(1, frame.sample_rate)
    duration_ms = round(samples * 1000.0 / rate, 3)
    return pcm, duration_ms, None


# ---------------------------------------------------------------------------
# Voice activity detection + utterance segmentation
# ---------------------------------------------------------------------------


def rms_linear16(pcm: bytes) -> float:
    """RMS energy of a linear16 PCM buffer (0 when silent/empty)."""
    n = len(pcm) // 2
    if not n:
        return 0.0
    total = 0
    for (value,) in struct.iter_unpack("<h", pcm):
        total += value * value
    return (total / n) ** 0.5


DEFAULT_SILENCE_THRESHOLD_RMS = 300.0   # ~ -40 dBFS of the 14-bit u-law range
DEFAULT_MIN_SPEECH_MS = 100.0           # shorter blips are noise, not speech
DEFAULT_SILENCE_MS = 450.0              # how much silence closes an utterance
MAX_SEGMENT_MS = 60_000.0               # force-close runaway utterances


@dataclass
class SegmentEvent:
    """speech.started or speech.ended for one utterance."""

    kind: str                        # speech.started | speech.ended
    start_ms: float = 0.0            # stream-relative utterance bounds
    end_ms: float = 0.0
    pcm: bytes = b""                 # the utterance's audio (linear16)
    duration_ms: float = 0.0

    def out(self) -> dict:
        return {"kind": self.kind, "start_ms": round(self.start_ms, 1),
                "end_ms": round(self.end_ms, 1), "duration_ms": round(self.duration_ms, 1),
                "audio_bytes": len(self.pcm)}


class UtteranceSegmenter:
    """Continuous chunk flow -> utterance segments (the unit ASR sees).

    Feed every decoded chunk (in order, with its duration). Emits:
    * ``speech.started`` once min_speech_ms of consecutive voiced audio
      accumulates (short blips stay noise),
    * ``speech.ended`` after silence_ms of quiet (or when the utterance
      exceeds max_ms), carrying the utterance's full PCM.

    Pure state; one segmenter per media stream.
    """

    def __init__(self, *, silence_threshold_rms: float = DEFAULT_SILENCE_THRESHOLD_RMS,
                 min_speech_ms: float = DEFAULT_MIN_SPEECH_MS,
                 silence_ms: float = DEFAULT_SILENCE_MS):
        self.silence_threshold_rms = float(silence_threshold_rms)
        self.min_speech_ms = float(min_speech_ms)
        self.silence_ms = float(silence_ms)
        self.stream_ms = 0.0            # total audio consumed so far
        self._in_speech = False
        self._started_emitted = False   # speech.started fires once per utterance
        self._speech_ms = 0.0           # voiced audio in the current utterance
        self._trailing_silence_ms = 0.0
        self._segment_start_ms = 0.0
        self._pcm: list[bytes] = []

    def feed(self, pcm: bytes, chunk_ms: float) -> list[SegmentEvent]:
        """Consume one chunk; return the segment events it completes."""
        events: list[SegmentEvent] = []
        start_ms = self.stream_ms
        self.stream_ms += chunk_ms
        voiced = rms_linear16(pcm) >= self.silence_threshold_rms

        if voiced:
            if not self._in_speech:
                self._in_speech = True
                self._segment_start_ms = start_ms
                self._speech_ms = 0.0
                self._trailing_silence_ms = 0.0
                self._pcm = []
                self._started_emitted = False
            self._speech_ms += chunk_ms
            self._trailing_silence_ms = 0.0
            self._pcm.append(pcm)
            if self._speech_ms >= self.min_speech_ms and not self._started_emitted:
                events.append(SegmentEvent(kind="speech.started",
                                           start_ms=self._segment_start_ms,
                                           end_ms=start_ms + chunk_ms))
                self._started_emitted = True
        else:
            if self._in_speech:
                self._trailing_silence_ms += chunk_ms
                self._pcm.append(pcm)
                if (self._trailing_silence_ms >= self.silence_ms
                        or (self.stream_ms - self._segment_start_ms) >= MAX_SEGMENT_MS):
                    events.append(self._close(start_ms + chunk_ms))
        return events

    def _close(self, at_ms: float) -> SegmentEvent:
        pcm = b"".join(self._pcm)
        event = SegmentEvent(kind="speech.ended", start_ms=self._segment_start_ms,
                             end_ms=at_ms, duration_ms=at_ms - self._segment_start_ms,
                             pcm=pcm)
        self._in_speech = False
        self._started_emitted = False
        self._speech_ms = 0.0
        self._trailing_silence_ms = 0.0
        self._pcm = []
        return event

    def flush(self) -> list[SegmentEvent]:
        """Stream ended mid-utterance: close what is open (or nothing)."""
        if self._in_speech:
            return [self._close(self.stream_ms)]
        return []


# ---------------------------------------------------------------------------
# The websocket-ASR contract: pluggable engines
# ---------------------------------------------------------------------------

# An ASR engine maps (pcm_linear16, sample_rate) -> asr result dict
# (transcript/confidence/is_final/language - validated by voice.validate_asr_result
# before anything downstream runs). Real engines bridge live providers;
# tests register deterministic fakes. NO engine = honest asr.unavailable.
ASREngine = Callable[[bytes, int], dict]

_ASR_ENGINES: dict[str, ASREngine] = {}


def register_asr_engine(name: str, engine: ASREngine) -> None:
    """Bind (or replace) an engine under a provider name (e.g. py8n_local)."""
    if not callable(engine):
        raise ValueError("an asr engine must be callable (pcm, sample_rate) -> result dict")
    _ASR_ENGINES[str(name).strip()] = engine


def unregister_asr_engine(name: str) -> bool:
    return _ASR_ENGINES.pop(str(name).strip(), None) is not None


def get_asr_engine(name: str) -> ASREngine | None:
    return _ASR_ENGINES.get(str(name).strip())


def registered_asr_engines() -> list[str]:
    return sorted(_ASR_ENGINES)


# ---------------------------------------------------------------------------
# Per-stream stats (the transport's honest bookkeeping)
# ---------------------------------------------------------------------------


@dataclass
class MediaStreamStats:
    """Counters for one media stream connection (session.context media block)."""

    stream_sid: str = ""
    chunks: int = 0
    audio_bytes: int = 0     # raw payload bytes received
    audio_ms: float = 0.0    # decoded audio duration
    skipped_frames: int = 0

    def snapshot(self) -> dict:
        return {"stream_sid": self.stream_sid, "chunks": self.chunks,
                "audio_bytes": self.audio_bytes, "audio_ms": round(self.audio_ms, 1),
                "skipped_frames": self.skipped_frames}
