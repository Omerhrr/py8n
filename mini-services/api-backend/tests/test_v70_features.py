"""V70 feature tests: voice audio transport (media streams / websocket ASR),
more providers (Telnyx Call Control for SIP+PSTN voice, WhatsApp interactive
buttons), and cross-process limit storage.

- media transport: the provider's JSON media dialect (connected/start/media/
  mark/stop) normalizes into frames, G.711 u-law decodes to linear16 with the
  CCITT reference vectors, RMS VAD + the UtteranceSegmenter cut the continuous
  chunk flow into UTTERANCES, and pluggable ASR engines transcribe them - the
  websocket endpoint runs the whole loop against a REAL session: speech
  events, asr.final, the SAME voice turns every other channel uses, barge-in
  when the caller speaks over an active utterance, honest asr.unavailable
  when no engine is bound, and honest skips for junk frames.
- telnyx_call_control: Call Control webhooks (SIP + PSTN ride the same
  events) map onto the v69 voice state machine, RFC 9421 message signatures
  verify with a real Ed25519 keypair (a covered content-digest BINDS the
  body), and the agent's call-control commands (answer/speak/...) build
  exactly and attempt honestly.
- whatsapp interactive: meta_build_interactive enforces Meta's documented
  limits (1..3 buttons, title <= 20 chars, ids unique), the tap comes back
  as the message (button_reply/list_reply/nfm_reply), and non-meta
  providers refuse buttons loudly instead of degrading silently.
- cross-process limits: the counters live in deployment_token_hits - one
  row per admitted request in the SHARED database. Two admits through two
  independent sessions (what two workers each open) enforce ONE limit; the
  refused row never commits; unlimited traffic still records history (so a
  policy applied later shapes the traffic already served); hits older than
  two days are pruned on admit.

Runs the FastAPI app in-process (httpx ASGITransport) plus starlette's
TestClient for the websocket handshake (the only client that can do it).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import struct
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.db import AsyncSessionLocal
from app.models import DeploymentTokenHit
from app.services import executor as executor_mod
from app.services import serving_limits
from app.services import channel_adapters as adapters
from app.services import voice_transport as transport

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v70-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v70 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _mk_echo_handler(client: httpx.AsyncClient, headers: dict, tag: str) -> dict:
    handler_code = (
        "env = input_data.get('payload', {})\n"
        "p = env.get('participant') or {}\n"
        "result = {'text': 'Hi ' + str(p.get('name', '')) + ', got: ' + str(env.get('text', ''))}\n"
    )
    res = await client.post("/workflows", headers=headers, json={"name": f"v70-handler-{tag}", "graph": {
        "nodes": [_node("t", "manual_trigger"), _node("reply", "code", {"code": handler_code})],
        "edges": [_edge("e1", "t", "reply")],
    }})
    assert res.status_code in (200, 201), res.text
    return res.json()


# ---------------------------------------------------------------------------
# helpers shared by the websocket test
# ---------------------------------------------------------------------------

def _pcm(samples: list[int]) -> str:
    return base64.b64encode(struct.pack(f"<{len(samples)}h", *samples)).decode()


SILENT_200MS = _pcm([0] * 1600)       # 1600 samples @ 8kHz = 200ms of silence
LOUD_200MS = _pcm([9000] * 1600)      # RMS 9000 - well over the VAD threshold


def _media(payload: str, chunk: int, encoding: str = "linear16") -> str:
    return json.dumps({"event": "media", "media": {
        "payload": payload, "track": "inbound", "chunk": chunk,
        "encoding": encoding, "sample_rate": 8000}})


class _WSClient:
    """A minimal ASGI websocket client that runs ON the test's event loop.

    starlette's TestClient drives the app from a worker THREAD's portal
    loop, which would make the media handler use pooled aiosqlite
    connections across two event loops - a recipe for 'database is
    locked'. Speaking the ASGI websocket spec directly keeps every DB
    touch of the test on ONE loop, exactly like production's single
    uvicorn loop.
    """

    def __init__(self, app, path: str, token: str | None = None):
        self._app = app
        qs = f"token={token}".encode() if token else b""
        self._scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "scheme": "ws", "path": path,
            "raw_path": path.encode(), "query_string": qs, "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("testclient", 50000), "server": ("testserver", 80),
            "subprotocols": [],
        }
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._outgoing: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def connect(self) -> None:
        # the ASGI websocket handshake: the server-side client MUST hand
        # the app a websocket.connect receive event first (starlette's
        # WebSocket.receive consumes it and transitions to CONNECTED)
        await self._incoming.put({"type": "websocket.connect"})
        self._task = asyncio.create_task(self._app(
            self._scope, self._incoming.get, self._outgoing.put))
        first = await self._outgoing.get()
        if first["type"] == "websocket.close":
            # refused before accept (4401/4404) - the endpoint returns now
            code = first.get("code", 1000)
            await self._task
            raise WebSocketDisconnect(code, first.get("reason", ""))
        assert first["type"] == "websocket.accept", first

    async def send_text(self, text: str) -> None:
        await self._incoming.put({"type": "websocket.receive", "text": text})

    async def receive_text(self) -> str:
        while True:
            try:
                msg = await asyncio.wait_for(self._outgoing.get(), 45)
            except asyncio.TimeoutError:
                if self._task is not None and self._task.done():
                    self._task.result()  # re-raise the endpoint's crash
                raise AssertionError("no websocket frame within 45s - "
                                     "the endpoint went silent ( hung or crashed)")
            if msg["type"] == "websocket.send":
                return msg.get("text") or ""
            if msg["type"] == "websocket.close":
                raise WebSocketDisconnect(msg.get("code", 1000), msg.get("reason", ""))
            # accept/other lifecycle frames are skipped

    async def close(self) -> None:
        if self._task is None:
            return
        await self._incoming.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self._task, 15)
        except (asyncio.TimeoutError, WebSocketDisconnect):
            pass
        self._task = None


# ---------------------------------------------------------------------------
# 1) the transport itself (pure): u-law, frames, VAD, segmentation, registry
# ---------------------------------------------------------------------------
def test_v70_media_transport_units():
    # ---- G.711 u-law: the CCITT reference vectors ----------------------
    assert transport.mulaw_to_linear(0x00) == -32124
    assert transport.mulaw_to_linear(0x80) == 32124
    assert transport.mulaw_to_linear(0xFF) == 0
    pcm = transport.mulaw_to_linear16(bytes([0x00, 0x80, 0xFF]))
    assert len(pcm) == 6  # one little-endian int16 per u-law byte
    assert struct.unpack("<3h", pcm) == (-32124, 32124, 0)

    # ---- frame parsing: the de-facto provider dialect ------------------
    start = {"event": "start", "start": {
        "streamSid": "SS1", "callSid": "CA9",
        "customParameters": {"encoding": "linear16", "sample_rate": "16000", "asr_engine": "deepgram_live"}}}
    frame, skip = transport.parse_media_frame(start)
    assert skip is None and frame.event == "start"
    assert (frame.stream_sid, frame.call_ref) == ("SS1", "CA9")
    assert (frame.encoding, frame.sample_rate) == ("linear16", 16000)
    assert frame.custom_parameters["asr_engine"] == "deepgram_live"

    media = {"event": "media", "media": {"payload": "aGk=", "track": "inbound", "chunk": "7"}}
    frame, skip = transport.parse_media_frame(media)
    assert skip is None and frame.payload_b64 == "aGk=" and frame.sequence == 7
    assert frame.encoding == "mulaw" and frame.sample_rate == 8000  # telephony defaults

    mark, _ = transport.parse_media_frame({"event": "mark", "mark": {"name": "after-greeting"}})
    assert mark.mark_name == "after-greeting"

    # honest skips: unknown events and unsupported encodings are counted,
    # not fatal; a non-object frame is a hard error (socket closes)
    _, skip = transport.parse_media_frame({"event": "keepalive"})
    assert skip["reason"] == "unknown_event"
    _, skip = transport.parse_media_frame({"event": "media", "media": {"payload": "x", "encoding": "amr-wb"}})
    assert skip["reason"] == "unsupported_encoding" and "linear16" in skip["detail"]
    _, skip = transport.parse_media_frame("not a dict")
    assert skip["reason"] == "not_an_object"

    # ---- audio decode: durations, bad payloads, odd bytes --------------
    frame, _ = transport.parse_media_frame({"event": "media", "media": {
        "payload": base64.b64encode(bytes(160)).decode()}})
    pcm, ms, skip = transport.decode_audio_chunk(frame)
    assert skip is None and len(pcm) == 320 and ms == 20.0  # 160 u-law bytes @ 8kHz

    frame, _ = transport.parse_media_frame({"event": "media", "media": {
        "payload": _pcm([100, 200, 300]), "encoding": "linear16"}})  # 6 bytes = 3 samples
    pcm, ms, skip = transport.decode_audio_chunk(frame)
    assert skip is None and len(pcm) == 6 and ms == 0.375  # 3 samples @ 8kHz

    odd = struct.pack("<3h", 100, 200, 300) + b"\x01"  # a trailing odd byte
    frame, _ = transport.parse_media_frame({"event": "media", "media": {
        "payload": base64.b64encode(odd).decode(), "encoding": "linear16"}})
    pcm, ms, skip = transport.decode_audio_chunk(frame)
    assert skip is None and len(pcm) == 6  # the odd byte is dropped honestly
    assert ms == 0.375

    frame, _ = transport.parse_media_frame({"event": "media", "media": {"payload": "!!!not-base64!!!"}})
    _, _, skip = transport.decode_audio_chunk(frame)
    assert skip["reason"] == "bad_base64"
    frame, _ = transport.parse_media_frame({"event": "media", "media": {"payload": ""}})
    _, _, skip = transport.decode_audio_chunk(frame)
    assert skip["reason"] == "empty_payload"

    # ---- VAD: RMS over linear16 ----------------------------------------
    assert transport.rms_linear16(b"") == 0.0
    assert transport.rms_linear16(struct.pack("<4h", 0, 0, 0, 0)) == 0.0
    assert transport.rms_linear16(struct.pack("<4h", 9000, 9000, 9000, 9000)) == 9000.0

    # ---- segmentation: blips stay noise, speech closes on silence ------
    SIL = struct.pack("<1600h", *([0] * 1600))
    BLIP = struct.pack("<480h", *([9000] * 480))    # 60ms of speech
    LOUD = struct.pack("<1600h", *([9000] * 1600))  # 200ms of speech

    seg = transport.UtteranceSegmenter()  # min_speech=100ms, silence=450ms
    assert seg.feed(SIL, 200.0) == []                   # stream_ms=200
    assert seg.feed(BLIP, 60.0) == []                   # blip opens silently: 60ms < 100ms min
    assert seg.feed(SIL, 200.0) == []                   # trailing silence 200 < 450
    events = seg.feed(LOUD, 200.0)                      # speech_ms=260 >= 100 -> started
    assert [(e.kind, e.start_ms) for e in events] == [("speech.started", 200.0)]
    assert seg.feed(SIL, 200.0) == []                   # trailing 200
    assert seg.feed(SIL, 200.0) == []                   # trailing 400 (< 450: still open)
    events = seg.feed(SIL, 200.0)                       # trailing 600 >= 450 -> closes
    assert len(events) == 1 and events[0].kind == "speech.ended"
    assert events[0].start_ms == 200.0 and events[0].end_ms == 1260.0
    assert events[0].duration_ms == 1060.0
    # the utterance's PCM: blip + silence + loud + 3 trailing silences
    assert len(events[0].pcm) == (480 + 1600 * 5) * 2
    assert seg.stream_ms == 1260.0
    # a closed segmenter opens cleanly for the next utterance
    events = seg.feed(LOUD, 200.0)
    assert [(e.kind, e.start_ms) for e in events] == [("speech.started", 1260.0)]

    # flush() closes an utterance the stream abandoned mid-speech
    seg2 = transport.UtteranceSegmenter()
    seg2.feed(SIL, 200.0)
    seg2.feed(LOUD, 200.0)
    events = seg2.flush()
    assert len(events) == 1 and events[0].kind == "speech.ended" and events[0].end_ms == 400.0
    assert len(events[0].pcm) == 1600 * 2  # only the speech chunk is the utterance
    assert transport.UtteranceSegmenter().flush() == []

    # ---- the ASR engine registry ---------------------------------------
    with pytest.raises(ValueError, match="must be callable"):
        transport.register_asr_engine("bad", "not callable")

    def engine(pcm: bytes, rate: int) -> dict:
        return {"transcript": "hi", "confidence": 1.0}

    transport.register_asr_engine("unit_engine", engine)
    assert transport.get_asr_engine("unit_engine") is engine
    assert transport.get_asr_engine("  unit_engine  ") is engine  # names trim
    assert "unit_engine" in transport.registered_asr_engines()
    assert transport.unregister_asr_engine("unit_engine") is True
    assert transport.unregister_asr_engine("unit_engine") is False
    assert transport.get_asr_engine("unit_engine") is None
    assert transport.MediaStreamStats(chunks=3, audio_ms=60.0).snapshot() == {
        "stream_sid": "", "chunks": 3, "audio_bytes": 0, "audio_ms": 60.0,
        "skipped_frames": 0}


# ---------------------------------------------------------------------------
# 2) the websocket media stream, end to end against a REAL session
# ---------------------------------------------------------------------------
def test_v70_media_stream_websocket():
    tag = uuid.uuid4().hex[:8]

    def fake_asr(pcm: bytes, sample_rate: int) -> dict:
        return {"transcript": "I want to order a laptop", "confidence": 0.91,
                "language": "en", "is_final": True}

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"media-{tag}", 1)
            h = _auth(user["token"])
            handler = await _mk_echo_handler(client, h, tag)
            res = await client.post("/voice/sessions", headers=h, json={
                "direction": "inbound", "provider": "twilio", "call_ref": f"CAmedia{tag}",
                "from_ref": "+234-700", "to_ref": "+234-701",
                "handler_workflow_id": handler["id"]})
            assert res.status_code == 201, res.text
            sess = res.json()
            res = await client.post(f"/voice/sessions/{sess['id']}/events", headers=h,
                                    json={"kind": "call.answered"})
            assert res.status_code == 200, res.text

            transport.register_asr_engine("py8n_local", fake_asr)
            try:
                ws = _WSClient(app, f"/api/v1/voice/sessions/{sess['id']}/media")
                await ws.connect()
                connected = json.loads(await ws.receive_text())
                assert connected["event"] == "connected" and connected["session_id"] == sess["id"]
                assert connected["state"] == "in_progress"
                assert "py8n_local" in connected["asr_engines"]

                await ws.send_text(json.dumps({"event": "start", "start": {
                    "streamSid": f"SS{tag}", "callSid": f"CAmedia{tag}",
                    "customParameters": {"encoding": "linear16", "sample_rate": 8000}}}))
                ack = json.loads(await ws.receive_text())
                assert ack["event"] == "stream_started" and ack["stream_sid"] == f"SS{tag}"

                # silence first: audio is measured, nothing fires back
                for i in range(3):
                    await ws.send_text(_media(SILENT_200MS, i))

                # the caller speaks: speech.started
                await ws.send_text(_media(LOUD_200MS, 10))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "speech.started" and ev["segment"]["audio_bytes"] == 0
                assert ev["segment"]["start_ms"] == 600.0

                # ...and silence closes the utterance: asr.final + the SAME turn
                for i in (11, 12, 13):
                    await ws.send_text(_media(SILENT_200MS, i))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "speech.ended" and ev["segment"]["duration_ms"] == 800.0
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "asr.final" and ev["asr"]["transcript"] == "I want to order a laptop"
                assert ev["asr"]["confidence"] == 0.91 and ev["asr"]["is_final"] is True
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "turn" and ev["reply"] == "Hi , got: I want to order a laptop"
                assert ev["tts"]["barge_in_ok"] is True and ev["tts"]["tts_id"]

                # BARGE-IN over the media stream: the caller speaks while the
                # agent's utterance is active -> the SAME barge-in primitive
                await ws.send_text(_media(LOUD_200MS, 20))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "barge_in" and ev["interrupted"] and ev["barge_in_count"] == 1
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "speech.started"

                # junk frames are honestly skipped, never fatal
                await ws.send_text(json.dumps({"event": "keepalive"}))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "skipped" and ev["reason"] == "unknown_event"
                await ws.send_text(json.dumps({"event": "media", "media": {
                    "payload": "!!!", "encoding": "linear16"}}))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "skipped" and ev["reason"] == "bad_base64"

                # no engine bound -> the transport NEVER invents words.
                # chunk 30 EXTENDS the still-open utterance (speech.started
                # fires once per utterance), so no new started comes back
                transport.unregister_asr_engine("py8n_local")
                await ws.send_text(_media(LOUD_200MS, 30))
                for i in (31, 32, 33):
                    await ws.send_text(_media(SILENT_200MS, i))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "speech.ended" and ev["segment"]["duration_ms"] == 1000.0
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "asr.unavailable" and "register_asr_engine" in ev["detail"]

                # marks echo back by name (also proves no turn frame followed)
                await ws.send_text(json.dumps({"event": "mark", "mark": {"name": "sync"}}))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "mark" and ev["name"] == "sync"

                # stop: final counters come back and the stream closes cleanly
                await ws.send_text(json.dumps({"event": "stop"}))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "stream_stopped"
                assert ev["stats"]["chunks"] == 12 and ev["stats"]["audio_ms"] == 2400.0
                assert ev["stats"]["skipped_frames"] == 2 and ev["stats"]["stream_sid"] == f"SS{tag}"
                await ws.close()

                # the session remembers: media context, the event timeline,
                # and the turn recorded on the LINKED conversation
                res = await client.get(f"/voice/sessions/{sess['id']}", headers=h)
                assert res.status_code == 200
                detail = res.json()
                media_ctx = detail.get("media") or {}
                assert media_ctx.get("stopped") is True and media_ctx.get("opened") is False
                assert media_ctx.get("stream_sid") == f"SS{tag}"
                kinds = [e["kind"] for e in detail.get("events") or []]
                for kind in ("media.stream_started", "speech.started", "speech.ended",
                             "asr.final", "barge_in", "media.stream_stopped"):
                    assert kind in kinds, f"{kind} missing from {kinds}"
                res = await client.get(f"/interactions/conversations/{detail['conversation_id']}",
                                       headers=h)
                msgs = [m for m in res.json()["messages"] if m["role"] in ("user", "agent")]
                assert [(m["role"], m["channel"]) for m in msgs] == [
                    ("user", "voice"), ("agent", "voice")]
                assert msgs[0]["payload"]["confidence"] == 0.91

                # ownership: a foreign token is refused before accept (4404)
                stranger = await _mk_user(client, f"media-{tag}", 3)
                with pytest.raises(WebSocketDisconnect):
                    await _WSClient(app, f"/api/v1/voice/sessions/{sess['id']}/media",
                                    token=stranger["token"]).connect()

                # an unknown session id is a 4404 too
                with pytest.raises(WebSocketDisconnect):
                    await _WSClient(app, "/api/v1/voice/sessions/does-not-exist/media").connect()
            finally:
                transport.unregister_asr_engine("py8n_local")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(serving_limits.reset_all())


# ---------------------------------------------------------------------------
# RFC 9421 test keypair + signature base (mirrors telnyx_verify_signature)
# ---------------------------------------------------------------------------

def _ed25519_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub_pem


def _rfc9421_headers(priv, raw: bytes, target: str, *, method: str = "POST",
                     components: tuple[str, ...] = ("@method", "@target", "content-digest")) -> dict:
    covered = " ".join(f'"{c}"' for c in components)
    sig_input = f'sig1=({covered});created=1618884473;keyid="k1"'
    lines = []
    for comp in components:
        if comp == "@method":
            lines.append(f'"@method": {method}')
        elif comp == "@target":
            lines.append(f'"@target": {target}')
        elif comp == "content-digest":
            digest = base64.b64encode(hashlib.sha256(raw).digest()).decode()
            lines.append(f'"content-digest": sha-256=:{digest}:')
    lines.append(f'"@signature-params": ({covered});created=1618884473;keyid="k1"')
    base = "\n".join(lines)
    sig = base64.b64encode(priv.sign(base.encode("utf-8"))).decode()
    headers = {"signature-input": sig_input, "signature": f"sig1=:{sig}:"}
    if "content-digest" in components:
        digest = base64.b64encode(hashlib.sha256(raw).digest()).decode()
        headers["content-digest"] = f"sha-256=:{digest}:"
    return headers


# ---------------------------------------------------------------------------
# 3) the telnyx_call_control adapter (pure)
# ---------------------------------------------------------------------------
def test_v70_telnyx_adapter():
    # ---- parse: SIP and PSTN ride the SAME call-control events ---------
    initiated = {"data": {"event_type": "call.initiated", "payload": {
        "call_control_id": "cc-1", "call_session_id": "cs-1", "direction": "incoming",
        "from": "sip:agent@sip.telnyx.com", "to": "sip:caller@external.sip",
        "client_state": "state-b64"}}}
    parsed = adapters.telnyx_parse_webhook(initiated)
    assert parsed.count == 1 and not parsed.skipped
    ev = parsed.events[0]
    assert ev.kind == "call.ringing" and ev.call_control_id == "cc-1"
    assert ev.from_ref.startswith("sip:") and ev.to_ref.startswith("sip:")

    answered = adapters.telnyx_parse_webhook(
        {"data": {"event_type": "call.answered", "payload": {"call_control_id": "cc-1"}}})
    assert answered.events[0].kind == "call.answered"

    # hangup causes map onto the honest endings
    for cause, end in (("NORMAL_CLEARING", "hangup"), ("NO_ANSWER", "no_answer"),
                       ("USER_BUSY", "busy"), ("SOMETHING_WEIRD", "failed")):
        parsed = adapters.telnyx_parse_webhook({"data": {"event_type": "call.hangup", "payload": {
            "call_control_id": "cc-1", "hangup_cause": cause}}})
        assert parsed.events[0].kind == end and parsed.events[0].end_kind == end
        assert parsed.events[0].hangup_cause == cause

    gather = adapters.telnyx_parse_webhook({"data": {"event_type": "call.gather.ended", "payload": {
        "call_control_id": "cc-1", "digits": "12"}}})
    assert gather.events[0].kind == "dtmf" and gather.events[0].digits == "12"
    empty = adapters.telnyx_parse_webhook({"data": {"event_type": "call.gather.ended", "payload": {
        "call_control_id": "cc-1", "digits": ""}}})
    assert empty.count == 0 and empty.skipped[0]["reason"] == "empty_gather"

    speak_on = adapters.telnyx_parse_webhook({"data": {"event_type": "call.speak.started", "payload": {
        "call_control_id": "cc-1"}}})
    assert speak_on.events[0].kind == "tts.started"
    speak_off = adapters.telnyx_parse_webhook({"data": {"event_type": "call.speak.ended", "payload": {
        "call_control_id": "cc-1"}}})
    assert speak_off.events[0].kind == "tts.ended"

    amd = adapters.telnyx_parse_webhook({"data": {"event_type": "call.machine.detection.ended", "payload": {
        "call_control_id": "cc-1", "result": "machine_greeting"}}})
    assert amd.events[0].kind == "voicemail_detected"
    human = adapters.telnyx_parse_webhook({"data": {"event_type": "call.machine.detection.ended", "payload": {
        "call_control_id": "cc-1", "result": "human"}}})
    assert human.count == 0 and human.skipped[0]["reason"] == "amd_human"

    fork = adapters.telnyx_parse_webhook({"data": {"event_type": "call.fork.started", "payload": {
        "call_control_id": "cc-1"}}})
    assert fork.count == 0 and "media" in fork.skipped[0]["detail"]
    unknown = adapters.telnyx_parse_webhook({"data": {"event_type": "call.not.a.thing", "payload": {}}})
    assert unknown.count == 0 and unknown.skipped[0]["reason"] == "unhandled_event_type"
    junk = adapters.telnyx_parse_webhook({"nope": 1})
    assert junk.count == 0 and junk.skipped[0]["reason"] == "unsupported_payload"
    missing_cc = adapters.telnyx_parse_webhook({"data": {"event_type": "call.answered", "payload": {}}})
    assert missing_cc.count == 0 and missing_cc.skipped[0]["reason"] == "no_call_control_id"

    # ---- verify: RFC 9421 with a REAL Ed25519 keypair ------------------
    priv, pub_pem = _ed25519_keypair()
    raw = json.dumps({"data": {"event_type": "call.initiated"}}).encode()
    target = "/api/v1/channels/telnyx/e1/webhook"
    headers = _rfc9421_headers(priv, raw, target)
    ok, detail = adapters.telnyx_verify_signature(pub_pem, headers, raw, method="POST", target=target)
    assert ok is True and detail is None

    # a tampered body fails even with a valid signature (content-digest BINDS)
    tampered = raw + b" "
    ok, detail = adapters.telnyx_verify_signature(pub_pem, headers, tampered, method="POST", target=target)
    assert ok is False and "content-digest" in detail

    # the wrong key, missing headers, and a mangled Signature-Input all fail
    _, other_pem = _ed25519_keypair()
    ok, detail = adapters.telnyx_verify_signature(other_pem, headers, raw, method="POST", target=target)
    assert ok is False
    ok, detail = adapters.telnyx_verify_signature(pub_pem, {}, raw, method="POST", target=target)
    assert ok is False and "Signature-Input" in detail
    bad_input = dict(headers, **{"signature-input": "sig1=no-parens-here"})
    ok, detail = adapters.telnyx_verify_signature(pub_pem, bad_input, raw, method="POST", target=target)
    assert ok is False and "RFC 9421" in detail
    # a covered header absent from the request fails loudly
    h2 = _rfc9421_headers(priv, raw, target, components=("@method", "x-custom"))
    ok, detail = adapters.telnyx_verify_signature(pub_pem, h2, raw, method="POST", target=target)
    assert ok is False and "x-custom" in detail

    # the registry dispatch routes telnyx too
    ok, _ = adapters.verify_request("telnyx_call_control", {"public_key": pub_pem},
                                    raw_body=raw, headers=headers, method="POST", target=target)
    assert ok is True
    assert adapters.PROVIDER_PATHS["telnyx_call_control"] == "telnyx"
    assert adapters.REQUIRED_CONFIG["telnyx_call_control"]["channel"] == "voice"
    assert "public_key" in adapters.REQUIRED_CONFIG["telnyx_call_control"]["secret"]
    assert "api_key" in adapters.REQUIRED_CONFIG["telnyx_call_control"]["credential"]

    # ---- build_command: the agent's side of the call -------------------
    cfg = {"api_key": "KEY"}
    cmd = adapters.telnyx_build_command(cfg, "cc-1", "answer")
    assert cmd["url"] == "https://api.telnyx.com/v2/calls/cc-1/actions/answer"
    assert cmd["headers"]["Authorization"] == "Bearer KEY" and cmd["json"] == {}

    cmd = adapters.telnyx_build_command(cfg, "cc-1", "speak", {"payload": "hello there", "voice": "female"})
    assert cmd["url"].endswith("/actions/speak")
    assert cmd["json"] == {"payload": "hello there", "voice": "female", "language": "en-US"}

    cmd = adapters.telnyx_build_command(cfg, "cc-1", "gather_using_audio",
                                        {"audio_url": "https://x/prompt.wav", "maximum_digits": 4})
    assert cmd["json"]["audio_url"] == "https://x/prompt.wav" and cmd["json"]["maximum_digits"] == 4
    assert cmd["json"]["terminating_digit"] == "#"

    cmd = adapters.telnyx_build_command(cfg, "cc-1", "transfer", {"to": "+234900"})
    assert cmd["json"] == {"to": "+234900"}
    cmd = adapters.telnyx_build_command(cfg, "cc-1", "hangup")
    assert cmd["json"] == {}

    with pytest.raises(ValueError, match="unknown telnyx command"):
        adapters.telnyx_build_command(cfg, "cc-1", "teleport")
    with pytest.raises(ValueError, match="call_control_id is required"):
        adapters.telnyx_build_command(cfg, "  ", "answer")
    with pytest.raises(ValueError, match="requires 'payload'"):
        adapters.telnyx_build_command(cfg, "cc-1", "speak", {})
    with pytest.raises(ValueError, match="requires 'audio_url'"):
        adapters.telnyx_build_command(cfg, "cc-1", "gather_using_audio", {})

    # api_key masks like every other secret
    masked = adapters.mask_config({"api_key": "KEY-123456", "public_key": pub_pem})
    assert masked["api_key"] == "KEY-...(10 chars)" and "KEY-123456" not in json.dumps(masked)


# ---------------------------------------------------------------------------
# 4) the telnyx receive path, end to end through the API
# ---------------------------------------------------------------------------
def test_v70_telnyx_webhook_e2e():
    tag = uuid.uuid4().hex[:8]
    priv, pub_pem = _ed25519_keypair()

    async def _setup():
        async with _client() as client:
            user = await _mk_user(client, f"tnx-{tag}", 1)
            h = _auth(user["token"])
            handler = await _mk_echo_handler(client, h, tag)
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": f"telnyx-{tag}", "provider": "telnyx_call_control",
                "handler_workflow_id": handler["id"],
                "config": {"public_key": pub_pem}})
            assert res.status_code == 201, res.text
            ep = res.json()
            path = f"/channels/telnyx/{ep['id']}/webhook"
            assert path in ep["webhook_url"]
            return user, ep, path

    user, ep, path = asyncio.run(_setup())
    h = _auth(user["token"])

    def _signed(payload: dict, *, key=priv, target: str | None = None):
        raw = json.dumps(payload).encode()
        # the server derives @target from request.url.path, which carries
        # the /api/v1 mount prefix even though the client posts without it
        return raw, _rfc9421_headers(key, raw, target or f"/api/v1{path}")

    async def _go():
        async with _client() as client:
            # missing secret refuses endpoint creation loudly
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "bad", "provider": "telnyx_call_control", "config": {}})
            assert res.status_code == 400 and "public_key" in res.json()["detail"]

            # ---- call.initiated: session found-or-created, answer built ---
            raw, headers = _signed({"data": {"event_type": "call.initiated", "payload": {
                "call_control_id": f"cc-{tag}", "call_session_id": f"cs-{tag}",
                "direction": "incoming", "from": "sip:caller@external.sip",
                "to": "sip:agent@sip.telnyx.com"}}})
            res = await client.post(path, content=raw, headers=headers)
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["ok"] is True and body["received"] == 1 and body["skipped"] == []
            handled = body["handled"][0]
            assert handled["created"] is True and handled["state"] == "ringing"
            assert "answer_built" in handled["actions"]
            assert handled["delivery"]["delivery"] == "skipped"  # no api_key: honest
            assert "api_key" in handled["delivery"]["detail"]
            session_id = handled["session_id"]

            # the session exists with the SIP refs and the bound handler
            res = await client.get(f"/voice/sessions/{session_id}", headers=h)
            assert res.status_code == 200
            sess = res.json()
            assert sess["provider"] == "telnyx" and sess["call_ref"] == f"cc-{tag}"
            assert sess["from"].startswith("sip:") and sess["to"].startswith("sip:")
            assert sess["state"] == "ringing"

            # a replay of the SAME call_control_id finds the session (no dupe)
            raw, headers = _signed({"data": {"event_type": "call.initiated", "payload": {
                "call_control_id": f"cc-{tag}", "direction": "incoming",
                "from": "sip:caller@external.sip", "to": "sip:agent@sip.telnyx.com"}}})
            res = await client.post(path, content=raw, headers=headers)
            assert res.json()["handled"][0]["created"] is False

            # ---- answered -> gather digits runs the SAME voice turn ------
            raw, headers = _signed({"data": {"event_type": "call.answered", "payload": {
                "call_control_id": f"cc-{tag}"}}, })
            res = await client.post(path, content=raw, headers=headers)
            assert res.json()["handled"][0]["state"] == "in_progress"

            raw, headers = _signed({"data": {"event_type": "call.gather.ended", "payload": {
                "call_control_id": f"cc-{tag}", "digits": "12"}}, })
            res = await client.post(path, content=raw, headers=headers)
            handled = res.json()["handled"][0]
            assert "turn_run" in handled["actions"] and "speak_built" in handled["actions"]
            assert handled["delivery"]["delivery"] == "skipped"
            assert handled["delivery"]["request"]["json"]["payload"] == "Hi , got: 12"
            # the digits live on the linked conversation, channel=voice
            res = await client.get(f"/interactions/conversations/{sess['conversation_id']}", headers=h)
            users = [m for m in res.json()["messages"] if m["role"] == "user"]
            assert users[-1]["text"] == "12" and users[-1]["channel"] == "voice"

            # ---- speak.started/ended drive the utterance bookkeeping ----
            raw, headers = _signed({"data": {"event_type": "call.speak.started", "payload": {
                "call_control_id": f"cc-{tag}"}}, })
            res = await client.post(path, content=raw, headers=headers)
            assert "error" not in res.json()["handled"][0]
            raw, headers = _signed({"data": {"event_type": "call.speak.ended", "payload": {
                "call_control_id": f"cc-{tag}"}}, })
            res = await client.post(path, content=raw, headers=headers)
            assert "utterance_closed" in res.json()["handled"][0]["actions"]

            # ---- hangup ends the call with the honest cause --------------
            raw, headers = _signed({"data": {"event_type": "call.hangup", "payload": {
                "call_control_id": f"cc-{tag}", "hangup_cause": "NORMAL_CLEARING"}}, })
            res = await client.post(path, content=raw, headers=headers)
            assert res.json()["handled"][0]["state"] == "ended"
            res = await client.get(f"/voice/sessions/{session_id}", headers=h)
            assert res.json()["state"] == "ended" and res.json()["end_reason"] == "hangup"

            # ---- verification: unsigned / wrong-key / tampered -> 401 ----
            raw, _ = _signed({"data": {"event_type": "call.initiated", "payload": {
                "call_control_id": "cc-x"}}})
            res = await client.post(path, content=raw)  # no headers at all
            assert res.status_code == 401
            other_key, _ = _ed25519_keypair()  # a DIFFERENT private key signs -> verify refuses
            raw, headers = _signed({"data": {"event_type": "call.initiated", "payload": {
                "call_control_id": "cc-x"}}}, key=other_key)
            res = await client.post(path, content=raw, headers=headers)
            assert res.status_code == 401

            # events_received counts every accepted webhook
            res = await client.get("/channels/endpoints", headers=h)
            mine = next(e for e in res.json()["endpoints"] if e["id"] == ep["id"])
            assert mine["events_received"] >= 7

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(serving_limits.reset_all())


# ---------------------------------------------------------------------------
# 5) WhatsApp interactive buttons: build, limits, taps, delivery refusal
# ---------------------------------------------------------------------------
def test_v70_whatsapp_interactive():
    cfg = {"phone_number_id": "PN1", "access_token": "TOKEN"}

    # ---- the exact Graph API request for reply buttons ------------------
    req = adapters.meta_build_interactive(cfg, "234801", "Pick a lane", [
        {"id": "sales", "title": "Talk to Sales"},
        {"id": "support", "title": "Support"}])
    assert req["method"] == "POST"
    assert req["url"] == "https://graph.facebook.com/v21.0/PN1/messages"
    assert req["headers"]["Authorization"] == "Bearer TOKEN"
    body = req["json"]
    assert body["messaging_product"] == "whatsapp" and body["to"] == "234801"
    assert body["type"] == "interactive"
    inter = body["interactive"]
    assert inter["type"] == "button" and inter["body"] == {"text": "Pick a lane"}
    assert [b["reply"]["id"] for b in inter["action"]["buttons"]] == ["sales", "support"]
    assert all(b["type"] == "reply" for b in inter["action"]["buttons"])
    # header/footer ride along, truncated at Meta's 60
    req = adapters.meta_build_interactive(cfg, "1", "b", [{"id": "a", "title": "A"}],
                                          header="H" * 80, footer="F" * 80)
    assert req["json"]["interactive"]["header"]["text"] == "H" * 60
    assert req["json"]["interactive"]["footer"]["text"] == "F" * 60

    # ---- Meta's documented limits are enforced EXACTLY ------------------
    with pytest.raises(ValueError, match="non-empty body"):
        adapters.meta_build_interactive(cfg, "1", "   ", [{"id": "a", "title": "A"}])
    with pytest.raises(ValueError, match="1024-char limit"):
        adapters.meta_build_interactive(cfg, "1", "x" * 1025, [{"id": "a", "title": "A"}])
    with pytest.raises(ValueError, match="1..3 buttons"):
        adapters.meta_build_interactive(cfg, "1", "b", [])
    with pytest.raises(ValueError, match="1..3 buttons"):
        adapters.meta_build_interactive(cfg, "1", "b", [{"id": str(i), "title": "t"} for i in range(4)])
    with pytest.raises(ValueError, match="20-char limit"):
        adapters.meta_build_interactive(cfg, "1", "b", [{"id": "a", "title": "t" * 21}])
    with pytest.raises(ValueError, match="256-char limit"):
        adapters.meta_build_interactive(cfg, "1", "b", [{"id": "i" * 257, "title": "t"}])
    with pytest.raises(ValueError, match="duplicated"):
        adapters.meta_build_interactive(cfg, "1", "b", [{"id": "a", "title": "A"}, {"id": "a", "title": "B"}])
    with pytest.raises(ValueError, match="requires both"):
        adapters.meta_build_interactive(cfg, "1", "b", [{"id": "", "title": "A"}])

    # ---- the tap IS the message: button/list/form replies parse ---------
    def _wa(messages: list[dict]) -> dict:
        return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "PN1"},
            "contacts": [{"wa_id": "234801", "profile": {"name": "Grace"}}],
            "messages": messages}}]}]}

    parsed = adapters.meta_parse_webhook(_wa([
        {"from": "234801", "id": "w1", "type": "interactive",
         "interactive": {"type": "button_reply", "button_reply": {"id": "sales", "title": "Talk to Sales"}}},
        {"from": "234801", "id": "w2", "type": "interactive",
         "interactive": {"type": "list_reply", "list_reply": {"id": "row_2", "title": "Premium Plan"}}},
        {"from": "234801", "id": "w3", "type": "interactive",
         "interactive": {"type": "nfm_reply", "nfm_reply": {
             "name": "lead_form", "response_json": "{\"email\": \"a@b.c\"}"}}},
        {"from": "234801", "id": "w4", "type": "button",
         "button": {"text": "Yes please", "payload": "quick-yes"}},
    ]))
    assert parsed.count == 4 and not parsed.skipped
    m0, m1, m2, m3 = parsed.messages
    assert (m0.text, m0.extra["interactive_type"], m0.extra["interactive_reply_id"]) == (
        "Talk to Sales", "button_reply", "sales")
    assert (m1.text, m1.extra["interactive_type"], m1.extra["interactive_reply_id"]) == (
        "Premium Plan", "list_reply", "row_2")
    assert (m2.text, m2.extra["interactive_type"], m2.extra["form_name"]) == (
        "{\"email\": \"a@b.c\"}", "nfm_reply", "lead_form")
    assert (m3.text, m3.extra["button_payload"]) == ("Yes please", "quick-yes")
    assert all(m.extra["phone_number_id"] == "PN1" for m in parsed.messages)


def test_v70_whatsapp_interactive_e2e():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"wa-{tag}", 1)
            h = _auth(user["token"])
            handler = await _mk_echo_handler(client, h, tag)
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": f"wa-{tag}", "provider": "meta_cloud_api",
                "handler_workflow_id": handler["id"],
                "config": {"phone_number_id": "PN1", "verify_token": f"vt-{tag}",
                           "app_secret": f"appsec-{tag}"}})
            assert res.status_code == 201, res.text
            ep = res.json()
            path = f"/channels/whatsapp/{ep['id']}/webhook"

            # a button tap arrives as a signed webhook and becomes a message
            tap_msg = {"from": "234801", "id": f"w-{tag}", "type": "interactive",
                       "interactive": {"type": "button_reply",
                                       "button_reply": {"id": "sales", "title": "Talk to Sales"}}}
            tap_value = {"metadata": {"phone_number_id": "PN1"},
                         "contacts": [{"wa_id": "234801", "profile": {"name": "Grace"}}],
                         "messages": [tap_msg]}
            payload = {"object": "whatsapp_business_account",
                       "entry": [{"changes": [{"value": tap_value}]}]}
            raw = json.dumps(payload).encode()
            sig = "sha256=" + hmac.new(f"appsec-{tag}".encode(), raw, hashlib.sha256).hexdigest()
            res = await client.post(path, content=raw,
                                    headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
            assert res.status_code == 200, res.text
            handled = res.json()["handled"][0]
            assert handled["reply"] == "Hi Grace, got: Talk to Sales"

            # preview-outbound with buttons: the exact interactive request
            res = await client.post(f"/channels/endpoints/{ep['id']}/preview-outbound", headers=h,
                                    json={"to": "234801", "text": "Pick a lane",
                                          "buttons": [{"id": "sales", "title": "Talk to Sales"}]})
            assert res.status_code == 200, res.text
            prev = res.json()
            buttons = prev["json"]["interactive"]["action"]["buttons"]
            assert buttons == [{"type": "reply", "reply": {"id": "sales", "title": "Talk to Sales"}}]
            assert prev["would_deliver"] is False  # no access_token credential

            # deliver with buttons without a credential: skipped honestly
            res = await client.post(f"/channels/endpoints/{ep['id']}/deliver", headers=h,
                                    json={"to": "234801", "text": "Pick a lane",
                                          "buttons": [{"id": "sales", "title": "Talk to Sales"}]})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["delivery"] == "skipped" and "access_token" in out["detail"]
            assert out["request"]["json"]["type"] == "interactive"

            # over-limit buttons are refused with the exact Meta rule
            res = await client.post(f"/channels/endpoints/{ep['id']}/preview-outbound", headers=h,
                                    json={"to": "1", "text": "x",
                                          "buttons": [{"id": str(i), "title": "t"} for i in range(4)]})
            assert res.status_code == 400 and "1..3 buttons" in res.json()["detail"]

            # NON-meta providers refuse buttons loudly instead of degrading
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": f"tg-{tag}", "provider": "telegram_bot_api",
                "handler_workflow_id": handler["id"],
                "config": {"secret_token": f"tgsec-{tag}"}})
            assert res.status_code == 201, res.text
            tg = res.json()
            res = await client.post(f"/channels/endpoints/{tg['id']}/preview-outbound", headers=h,
                                    json={"to": "42", "text": "hi", "buttons": [{"id": "a", "title": "A"}]})
            assert res.status_code == 400 and "interactive buttons" in res.json()["detail"]
            res = await client.post(f"/channels/endpoints/{tg['id']}/deliver", headers=h,
                                    json={"to": "42", "text": "hi", "buttons": [{"id": "a", "title": "A"}]})
            assert res.status_code == 400 and "interactive buttons" in res.json()["detail"]
            # plain text delivery on telegram is unaffected
            res = await client.post(f"/channels/endpoints/{tg['id']}/preview-outbound", headers=h,
                                    json={"to": "42", "text": "hi"})
            assert res.status_code == 200 and "sendMessage" in res.json()["url"]

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(serving_limits.reset_all())


# ---------------------------------------------------------------------------
# 6) cross-process limit storage: the database IS the counter
# ---------------------------------------------------------------------------
def test_v70_cross_process_limits():
    tag = uuid.uuid4().hex[:8]

    async def _mk_setup():
        async with _client() as client:
            user = await _mk_user(client, f"lim-{tag}", 1)
            h = _auth(user["token"])
            # a tiny LM deployment as the serving target
            params = {"text_column": "doc", "d_model": 16, "epochs": 2, "model_name": f"lim-model-{tag}"}
            res = await client.post("/workflows", headers=h, json={"name": f"lm-{tag}", "graph": {
                "nodes": [_node("t", "manual_trigger"), _node("lm", "lm_train", params)],
                "edges": [_edge("e1", "t", "lm")]}})
            wf = res.json()
            res = await client.post(f"/workflows/{wf['id']}/run", headers=h,
                                    json={"payload": {"items": [
                                        {"doc": "the agent resolved the ticket quickly"} for _ in range(12)]}})
            assert res.status_code in (200, 202), res.text
            exec_id = res.json()["execution_id"]
            for _ in range(400):
                res = await client.get(f"/executions/{exec_id}", headers=h)
                if res.json()["status"] not in ("running", "queued"):
                    break
                await asyncio.sleep(0.05)
            assert res.json()["status"] == "success", str(res.json().get("error"))[:300]
            res = await client.post("/deployments", headers=h, json={
                "name": f"dep-{tag}", "model": f"lim-model-{tag}", "environment": "dev"})
            assert res.status_code == 201, res.text
            dep = res.json()
            return user, dep, dep["workflow"]["id"]

    user, dep, wf_id = asyncio.run(_mk_setup())
    h = _auth(user["token"])

    async def _hit_rows(token_id: str) -> int:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(DeploymentTokenHit)
                .where(DeploymentTokenHit.token_id == token_id))).scalars().all()
            return len(rows)

    async def _go():
        async with _client() as client:
            # ---- rate shaping through the webhook, exactly like v69 ------
            res = await client.post(f"/deployments/{dep['id']}/tokens", headers=h,
                                    json={"name": "shaped", "rate_per_min": 2})
            shaped = res.json()
            call = {"Authorization": f"Bearer {shaped['token']}"}
            for i in range(2):
                res = await client.post(f"/webhooks/{wf_id}", json={"prompt": f"hi {i}"}, headers=call)
                assert res.status_code == 200, res.text
            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "hi"}, headers=call)
            assert res.status_code == 429 and "rate limit exceeded" in res.json()["detail"]
            assert res.headers["X-RateLimit-Limit"] == "2"
            assert res.headers["X-RateLimit-Remaining"] == "0"
            assert int(res.headers["Retry-After"]) >= 1

            # v70: the two admitted requests ARE rows in the shared table;
            # the refused request is NOT (the overflow row was rolled back)
            assert await _hit_rows(shaped["id"]) == 2

            # ---- the CROSS-PROCESS proof: a second, independent session -
            # two workers each open their own session/connection; whichever
            # process admits next must see the first one's rows. The policy
            # read and the admit below use a session this process never
            # shared with the webhook path - the DB is the only witness.
            async with AsyncSessionLocal() as fresh:
                policy = await serving_limits.policy_for_token(fresh, shaped["id"])
            assert policy is not None and policy.rate_per_min == 2
            with pytest.raises(serving_limits.LimitExceeded) as exc_info:
                await serving_limits.admit(shaped["id"], policy)
            assert "rate limit exceeded" in exc_info.value.detail
            assert exc_info.value.headers["X-RateLimit-Limit"] == "2"
            # the direct refusal left no row either
            assert await _hit_rows(shaped["id"]) == 2

            # ...and usage_snapshot from ANOTHER session reads the same rows
            usage = await serving_limits.usage_snapshot(shaped["id"], policy)
            assert usage["minute_used"] == 2 and usage["minute_remaining"] == 0

            # ---- a quota exhausts across sessions too --------------------
            res = await client.post(f"/deployments/{dep['id']}/tokens", headers=h,
                                    json={"name": "quotal", "daily_quota": 2})
            quotal = res.json()
            qcall = {"X-Deployment-Token": quotal["token"]}
            for _ in range(2):
                res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "hi"}, headers=qcall)
                assert res.status_code == 200, res.text
            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "hi"}, headers=qcall)
            assert res.status_code == 429 and "daily quota exhausted" in res.json()["detail"]
            assert res.headers["X-Quota-Used"] == "2" and res.headers["X-Quota-Limit"] == "2"
            assert "T00:00:00" in res.headers["X-Quota-Reset"]
            assert await _hit_rows(quotal["id"]) == 2

            # the usage endpoint reads the SHARED table (not private state)
            res = await client.get(f"/deployments/{dep['id']}/tokens/{quotal['id']}/usage", headers=h)
            usage = res.json()["usage"]
            assert usage["day_used"] == 2 and usage["minute_used"] == 2

            # ---- unlimited traffic still records history -----------------
            res = await client.post(f"/deployments/{dep['id']}/tokens", headers=h,
                                    json={"name": "free"})
            free = res.json()
            fcall = {"Authorization": f"Bearer {free['token']}"}
            for _ in range(3):
                res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "hi"}, headers=fcall)
                assert res.status_code == 200, res.text
            assert await _hit_rows(free["id"]) == 3

            # applying a policy NOW shapes the traffic the token ALREADY
            # served this window - the same semantic v69's in-process
            # windows gave, now from the shared table
            res = await client.put(f"/deployments/{dep['id']}/tokens/{free['id']}/limits",
                                   headers=h, json={"rate_per_min": 1})
            assert res.status_code == 200 and res.json()["limits"]["rate_per_min"] == 1
            assert res.json()["usage"]["minute_used"] == 3
            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "hi"}, headers=fcall)
            assert res.status_code == 429  # the 4th request this minute is over 1/min
            res = await client.put(f"/deployments/{dep['id']}/tokens/{free['id']}/limits",
                                   headers=h, json={"rate_per_min": None, "daily_quota": None})
            assert res.json()["limits"] == {"rate_per_min": None, "daily_quota": None}

            # ---- the SSE stream writes the same shared rows --------------
            res = await client.post(f"/deployments/{dep['id']}/tokens", headers=h,
                                    json={"name": "streamer", "rate_per_min": 1})
            streamer = res.json()
            scall = {"Authorization": f"Bearer {streamer['token']}"}
            res = await client.post(f"/deployments/{dep['id']}/stream", headers=scall,
                                    json={"prompt": "hi", "max_tokens": 4})
            assert res.status_code == 200, res.text
            assert await _hit_rows(streamer["id"]) == 1
            res = await client.post(f"/deployments/{dep['id']}/stream", headers=scall,
                                    json={"prompt": "hi", "max_tokens": 4})
            assert res.status_code == 429 and "rate limit exceeded" in res.json()["detail"]
            assert await _hit_rows(streamer["id"]) == 1  # refusal stored nothing

            # ---- pruning: hits older than two days die on the next admit -
            old_day = datetime.now(timezone.utc) - timedelta(days=3)
            async with AsyncSessionLocal() as db:
                db.add_all([
                    DeploymentTokenHit(token_id=streamer["id"], admitted_at=old_day,
                                       quota_day=old_day.strftime("%Y-%m-%d")),
                    DeploymentTokenHit(token_id=streamer["id"], admitted_at=old_day,
                                       quota_day=old_day.strftime("%Y-%m-%d"))])
                await db.commit()
            assert await _hit_rows(streamer["id"]) == 3  # 1 admitted + 2 stale
            await serving_limits.admit(streamer["id"], None)  # unlimited admit still prunes
            assert await _hit_rows(streamer["id"]) == 2  # the stale rows are gone

            # reset_all empties the table (tests between scenarios)
            await serving_limits.reset_all()
            async with AsyncSessionLocal() as db:
                assert (await db.execute(select(DeploymentTokenHit))).scalars().all() == []

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        asyncio.run(serving_limits.reset_all())
