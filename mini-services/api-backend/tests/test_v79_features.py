"""V79 feature tests: recording/transcription archives for meetings, the
browser meeting client's backend surface, and the SMS auto-answer.

* RECORDING ARCHIVES: a meeting opens ONE recording pass at a time
  (POST /meetings/{id}/recordings); the room is told on every leg's
  timeline (recording.started / recording.stopped); the web legs'
  utterance audio accumulates in the in-process capture registry as the
  media websocket decodes it (real utterances through the real VAD over
  the ASGI websocket), and STOP writes the archive - the merged
  transcript snapshot (markdown), the chat log (json) and each captured
  leg's utterance audio (a REAL RIFF/WAV artifact) - all regular
  artifacts, pointed to by the recording row. Ending the meeting stops
  the active recording automatically; a leg that joins a recorded room
  starts capturing on join.
* SMS AUTO-ANSWER: a caller who is WAITING in a queue with the SMS
  backchannel + auto-answer enabled REPLIES the queue's own text - the
  keyword ("1") intercepts the inbound SMS BEFORE the conversation
  layer: the entry leaves the line (action=callback composes the v78
  callback campaign - keep the place, end the hold with reason=callback,
  book the target; action=abandon releases the call), the confirmation
  text goes back through the SAME endpoint, and every step lands in the
  entry's backchannel log and on the session timeline. Non-keyword text
  and senders who are not waiting ride the normal conversation path.

Runs the FastAPI app in-process (httpx ASGITransport); the websocket
scenarios speak the ASGI spec directly so every DB touch stays on the
test's single event loop. No network egress.
"""

from __future__ import annotations

import asyncio
import hmac as _hmac
import json

import httpx
import pytest

from app.main import app

API = "http://testserver/api/v1"


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    from app.api import _ratelimit

    _ratelimit.reset_all()
    yield
    _ratelimit.reset_all()


@pytest.fixture(autouse=True)
def _clean_captures():
    """The capture registry is process state - keep scenarios isolated."""
    from app.services import voice_recordings as rec_svc

    rec_svc._captures.clear()
    yield
    rec_svc._captures.clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod

    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _sync(coro):
    return asyncio.run(coro)


async def _wrap(coro):
    try:
        return await coro
    finally:
        await _drain_background()


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int = 1) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v79-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v79 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_agent(client: httpx.AsyncClient, headers: dict, name: str,
                    greeting: str = "You have reached the room.") -> dict:
    res = await client.post("/voice/agents", headers=headers, json={
        "name": name, "scaffold_handler": True, "greeting_text": greeting})
    assert res.status_code == 201, res.text
    return res.json()


async def _seed_timeline(session_id: str, *events: tuple[str, dict]) -> None:
    """Seed transcript-shape events straight onto a leg's timeline (the
    same kinds the real transport and voice_turn write - asr.final has no
    HTTP door, it lands over the media websocket inside a full turn)."""
    from app.db import AsyncSessionLocal
    from app.models import VoiceSession
    from app.services import voice as voice_svc

    async with AsyncSessionLocal() as db:
        row = await db.get(VoiceSession, session_id)
        assert row is not None
        for kind, payload in events:
            await voice_svc._add_event(db, row, kind, payload)
        await db.commit()


async def _mk_meeting(client: httpx.AsyncClient, headers: dict, agent_id: str,
                      labels: tuple[str, ...] = ()) -> tuple[dict, list[dict]]:
    res = await client.post("/voice/meetings", headers=headers,
                            json={"title": "v79 room", "agent_id": agent_id})
    assert res.status_code == 201, res.text
    meeting = res.json()
    legs = []
    for label in labels:
        res = await client.post(f"/voice/meetings/{meeting['id']}/join",
                                headers=headers, json={"label": label})
        assert res.status_code == 200, res.text
        legs.append(res.json()["participant"])
    return meeting, legs


async def _mk_live_session(client: httpx.AsyncClient, headers: dict,
                           frm: str = "+15550001111") -> dict:
    res = await client.post("/voice/sessions", headers=headers, json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"cc-{frm}",
        "from_ref": frm, "to_ref": "+15550002222"})
    assert res.status_code == 201, res.text
    sid = res.json()["id"]
    for kind in ("call.ringing", "call.answered"):
        res = await client.post(f"/voice/sessions/{sid}/events", headers=headers,
                                json={"kind": kind, "payload": {}})
        assert res.status_code == 200, res.text
    res = await client.get(f"/voice/sessions/{sid}", headers=headers)
    return res.json()


def _sms_sign(secret: str, raw: bytes) -> str:
    from app.services import channel_adapters as adapters

    return adapters.generic_sms_sign(secret, raw)


def _sms_post(ep: dict, payload: dict, secret: str) -> dict:
    body = json.dumps(payload).encode()
    assert ep["webhook_url"].startswith("/api/v1/")
    return {"url": ep["webhook_url"][len("/api/v1"):], "content": body,
            "headers": {"content-type": "application/json",
                        "x-py8n-signature": _sms_sign(secret, body)}}


# the v70/v77/v78 ASGI websocket client - verbatim pattern, one event loop
class _WSClient:
    def __init__(self, app_, path: str, token: str | None = None):
        self._app = app_
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
        await self._incoming.put({"type": "websocket.connect"})
        self._task = asyncio.create_task(self._app(
            self._scope, self._incoming.get, self._outgoing.put))
        first = await self._outgoing.get()
        if first["type"] == "websocket.close":
            code = first.get("code", 1000)
            await self._task
            raise RuntimeError(f"websocket closed {code}")
        assert first["type"] == "websocket.accept", first

    async def send_text(self, text: str) -> None:
        await self._incoming.put({"type": "websocket.receive", "text": text})

    async def send_json(self, frame: dict) -> None:
        await self.send_text(json.dumps(frame))

    async def receive_json(self) -> dict:
        while True:
            try:
                msg = await asyncio.wait_for(self._outgoing.get(), 45)
            except asyncio.TimeoutError:
                if self._task is not None and self._task.done():
                    self._task.result()
                raise AssertionError("no websocket frame within 45s - "
                                     "the endpoint went silent (hung or crashed)")
            if msg["type"] == "websocket.send":
                return json.loads(msg.get("text") or "{}")
            if msg["type"] == "websocket.close":
                raise RuntimeError(f"websocket closed {msg.get('code', 1000)}")

    async def close(self) -> None:
        if self._task is None:
            return
        await self._incoming.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self._task, 15)
        except (asyncio.TimeoutError, RuntimeError):
            pass
        self._task = None


# ---------------------------------------------------------------------------
# 1) pure units: the WAV writer + the capture registry
# ---------------------------------------------------------------------------

def test_v79_recording_units():
    import struct

    from app.services import voice_recordings as rec

    # real RIFF/WAVE bytes: 1000 linear16 samples at 16k -> 2000 bytes data
    pcm = struct.pack("<1000h", *([900] * 1000))
    wav = rec.pcm_to_wav(pcm, 16000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert len(wav) == 44 + 2000  # canonical header + data

    # the capture registry: start -> append -> pop
    assert rec.start_capture("s1", "rec1") is True
    assert rec.start_capture("s1", "rec1") is False      # already capturing
    assert rec.append_utterance("s1", pcm, 62.5, 16000) is True
    assert rec.append_utterance("ghost", pcm, 62.5, 16000) is False
    assert rec.append_utterance("s1", b"", 1.0, 16000) is False  # empty audio
    # a mid-stream sample-rate change is refused (it would corrupt the WAV)
    assert rec.append_utterance("s1", pcm, 62.5, 8000) is False
    snap = rec.capture_snapshot("s1")
    assert snap["utterances"] == 1 and snap["pcm_bytes"] == 2000
    assert snap["skipped"] == 1 and snap["sample_rate"] == 16000
    cap = rec.pop_capture("s1")
    assert cap["utterances"] == 1 and cap["recording_id"] == "rec1"
    assert rec.pop_capture("s1") is None
    assert rec.stop_captures_for_recording("rec9") == []

    # the honest ceiling: a capture over its byte cap skips and counts
    rec.start_capture("s2", "rec2")
    from unittest.mock import patch

    with patch.object(rec, "MAX_PCM_BYTES_PER_SESSION", 10):
        assert rec.append_utterance("s2", pcm, 62.5, 16000) is False
    assert rec.capture_snapshot("s2")["skipped"] == 1
    rec.pop_capture("s2")


# ---------------------------------------------------------------------------
# 2) the archive E2E: words + chat + web-leg utterance audio, real WAVs
# ---------------------------------------------------------------------------

def test_v79_recording_archive_e2e():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "rec")
            h = _auth(user["token"])
            # a SILENT persona: no greeting line, the transcript counts stay exact
            agent = await _mk_agent(client, h, "room agent rec", greeting="")
            meeting, legs = await _mk_meeting(client, h, agent["id"],
                                              labels=("ada", "bob"))
            m_id = meeting["id"]

            # ---- starting: the room is told, one pass at a time ---------
            res = await client.post(f"/voice/meetings/{m_id}/recordings",
                                    headers=h, json={"name": "standup archive"})
            assert res.status_code == 201, res.text
            rec = res.json()
            assert rec["state"] == "recording" and rec["name"] == "standup archive"
            assert sorted(rec["capture_started_sessions"]) == \
                sorted(l["session_id"] for l in legs if l["channel"] == "web")
            res = await client.post(f"/voice/meetings/{m_id}/recordings",
                                    headers=h, json={})
            assert res.status_code == 400
            assert "already has a recording" in res.json()["detail"]

            # every joined leg's timeline learned the room is recorded
            for leg in legs:
                res = await client.get(f"/voice/sessions/{leg['session_id']}", headers=h)
                kinds = [e["kind"] for e in res.json()["events"]]
                assert "recording.started" in kinds

            # ---- the room talks: transcript lines + chat ---------------
            # asr.final has no HTTP door (it lands inside a full voice turn
            # over the media websocket) - the timeline is seeded with the
            # same kinds the transport writes, then the archive reads them
            await _seed_timeline(
                legs[0]["session_id"],
                ("asr.final", {"transcript": "hello room, ada speaking", "confidence": 0.91}),
                ("asr.final", {"transcript": "and bob agrees", "confidence": 0.87}))
            await _seed_timeline(
                legs[1]["session_id"],
                ("tts.started", {"text": "the agent says hi", "source": "turn"}))
            for text in ("agenda anyone?", "one moment, sharing notes"):
                res = await client.post(f"/voice/meetings/{m_id}/chat", headers=h,
                                        json={"participant_id": legs[0]["id"],
                                              "author": "ada", "text": text})
                assert res.status_code == 200, res.text

            # ---- REAL audio over the REAL websocket: ada's leg ---------
            ws = _WSClient(app, f"/api/v1/voice/sessions/{legs[0]['session_id']}/media",
                           token=user["token"])
            await ws.connect()
            connected = await ws.receive_json()
            assert connected["event"] == "connected"
            await ws.send_json({"event": "start", "start": {
                "streamSid": "py8n-test", "callSid": legs[0]["session_id"],
                "customParameters": {"encoding": "linear16", "sample_rate": 16000}}})
            frames = [await ws.receive_json() for _ in range(1)]
            assert frames[0]["event"] == "stream_started"

            import base64
            import struct as st

            def _pcm_b64(samples: list[int]) -> str:
                return base64.b64encode(st.pack(f"<{len(samples)}h", *samples)).decode()

            silence = _pcm_b64([0] * 3200)   # 200ms @ 16k
            loud = _pcm_b64([9000] * 3200)   # 200ms @ 16k, well over the VAD
            for chunk, seq in ((silence, 1), (loud, 2), (loud, 3), (loud, 4),
                               (silence, 5), (silence, 6), (silence, 7)):
                await ws.send_json({"event": "media",
                                    "media": {"payload": chunk, "track": "inbound",
                                              "chunk": seq, "encoding": "linear16",
                                              "sample_rate": 16000}})
            # drain until the utterance closes (speech.ended) - the capture
            # hook has already eaten the PCM by then
            got_ended = False
            for _ in range(30):
                f = await ws.receive_json()
                if f.get("event") == "speech.ended":
                    got_ended = True
                    break
            assert got_ended, "the VAD never closed the utterance over the real transport"
            await ws.close()

            # ---- stop: the archive is written ---------------------------
            res = await client.post(
                f"/voice/meetings/{m_id}/recordings/{rec['id']}/stop", headers=h)
            assert res.status_code == 200, res.text
            archive = res.json()
            assert archive["state"] == "stopped"
            assert archive["counts"]["transcript_lines"] == 3
            assert archive["counts"]["chat_lines"] == 2
            audio = archive["artifacts"]["audio"]
            assert len(audio) == 2
            by_sid = {a["session_id"]: a for a in audio}
            assert by_sid[legs[0]["session_id"]]["captured"] is True
            assert by_sid[legs[0]["session_id"]]["utterances"] == 1
            assert by_sid[legs[0]["session_id"]]["duration_ms"] >= 600.0
            assert by_sid[legs[1]["session_id"]]["captured"] is False
            assert "no utterances" in by_sid[legs[1]["session_id"]]["detail"]

            # the transcript artifact is REAL markdown with the speakers in it
            t_id = archive["artifacts"]["transcript"]
            res = await client.get(f"/artifacts/{t_id}/content", headers=h)
            assert res.status_code == 200, res.text
            assert res.headers["content-type"].startswith("text/markdown")
            md = res.text
            assert "# standup archive" in md and "## Transcript" in md and "## Chat" in md
            assert "ada" in md and "hello room, ada speaking" in md
            assert "the agent says hi" in md and "agenda anyone?" in md

            # the chat artifact is REAL json
            c_id = archive["artifacts"]["chat"]
            res = await client.get(f"/artifacts/{c_id}/content", headers=h)
            chat_json = res.json()
            assert [m["text"] for m in chat_json["messages"]] == \
                ["agenda anyone?", "one moment, sharing notes"]

            # the leg's audio artifact is a REAL wav
            a_id = by_sid[legs[0]["session_id"]]["artifact_id"]
            res = await client.get(f"/artifacts/{a_id}/content", headers=h)
            assert res.status_code == 200, res.text
            assert res.headers["content-type"].startswith("audio/wav")
            raw = res.content
            assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
            assert len(raw) > 44 + 3000

            # the detail view (include=true) embeds the archived words
            res = await client.get(f"/voice/recordings/{rec['id']}?include=true", headers=h)
            assert res.status_code == 200, res.text
            detail = res.json()
            assert "hello room, ada speaking" in (detail["transcript"] or "")
            assert detail["chat"]["messages"][0]["author"] == "ada"

            # the shelf: list for the meeting + the owner-wide list
            res = await client.get(f"/voice/meetings/{m_id}/recordings", headers=h)
            assert [r["id"] for r in res.json()["recordings"]] == [rec["id"]]
            res = await client.get("/voice/recordings", headers=h)
            assert any(r["id"] == rec["id"] for r in res.json()["recordings"])

            # stopping twice refuses honestly
            res = await client.post(
                f"/voice/meetings/{m_id}/recordings/{rec['id']}/stop", headers=h)
            assert res.status_code == 400
            assert "already stopped" in res.json()["detail"]

            # another owner cannot read someone else's archive
            other = await _mk_user(client, "rec", n=2)
            res = await client.get(f"/voice/recordings/{rec['id']}",
                                   headers=_auth(other["token"]))
            assert res.status_code == 404
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3) join-into-recording + the auto-stop on meeting end
# ---------------------------------------------------------------------------

def test_v79_recording_room_lifecycle():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "rec2")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "room agent rec2")
            meeting, legs = await _mk_meeting(client, h, agent["id"], labels=("ada",))
            m_id = meeting["id"]

            res = await client.post(f"/voice/meetings/{m_id}/recordings",
                                    headers=h, json={})
            assert res.status_code == 201, res.text
            rec = res.json()

            # a leg joining a RECORDED room starts capturing on join
            res = await client.post(f"/voice/meetings/{m_id}/join", headers=h,
                                    json={"label": "late carol"})
            assert res.status_code == 200, res.text
            late = res.json()["participant"]
            assert res.json()["recording"]["capturing"] is True
            assert res.json()["recording"]["recording_id"] == rec["id"]

            # ending the room stops the active recording automatically (the
            # room cannot outlive its archive)
            res = await client.post(f"/voice/meetings/{m_id}/end", headers=h)
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["recording"]["id"] == rec["id"]
            assert out["recording"]["state"] == "stopped"
            res = await client.get(f"/voice/meetings/{m_id}/recordings", headers=h)
            rows = res.json()["recordings"]
            assert len(rows) == 1 and rows[0]["state"] == "stopped"
            assert rows[0]["meta"]["stop_reason"] == "meeting_ended"
            # the late leg's capture was flushed with the stop (no audio)
            audio = rows[0]["artifacts"]["audio"]
            assert {a["session_id"] for a in audio} == \
                {legs[0]["session_id"], late["session_id"]}

            # a dead meeting refuses new recordings
            res = await client.post(f"/voice/meetings/{m_id}/recordings",
                                    headers=h, json={})
            assert res.status_code == 400
            assert "already ended" in res.json()["detail"]
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4) the SMS auto-answer: reply "1" leaves the line - via callback
# ---------------------------------------------------------------------------

def test_v79_sms_auto_answer_callback():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "auto1")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "line agent auto1")

            # the any-gateway SMS channel (no outbound credentials - the
            # confirmation send is an honest skipped record, still recorded)
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "any-gateway auto", "provider": "generic_sms",
                "config": {"secret": "sms-sekrit-1"}})
            assert res.status_code == 201, res.text
            ep = res.json()

            # the support line: SMS backchannel + auto-answer + callbacks,
            # bound to the line's agent (the callback campaign composes on it)
            res = await client.post("/voice/queues", headers=h, json={
                "name": "auto line", "agent_id": agent["id"],
                "config": {"sms": {"enabled": True, "channel_id": ep["id"],
                                   "auto_answer": {"enabled": True, "keyword": "1",
                                                   "action": "callback"}},
                           "callback": {"enabled": True}}})
            assert res.status_code == 201, res.text
            queue = res.json()
            auto = queue["config"]["sms"]["auto_answer"]
            assert auto == {"enabled": True, "keyword": "1", "action": "callback",
                            "reply_template": ""}

            # a caller waits (position 1 of 1), the hold is real
            session = await _mk_live_session(client, h, frm="+15550007777")
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": session["id"]})
            assert res.status_code == 201, res.text
            entry_id = res.json()["entry_id"]

            # ---- the caller TEXTS "1" - the auto-answer intercepts ------
            posted = _sms_post(ep, {"from": "+15550007777", "to": "+15550002222",
                                    "text": " 1 "}, "sms-sekrit-1")
            res = await client.post(posted["url"], content=posted["content"],
                                    headers=posted["headers"])
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["received"] == 1
            handled = out["handled"][0]
            assert handled["auto_answer"] is True
            assert handled["queue_name"] == "auto line"
            assert handled["action"]["done"] is True
            assert handled["action"]["action"] == "callback"
            assert handled["action"]["campaign_id"] and handled["action"]["target_id"]
            # the confirmation text rendered the default callback template
            assert "off hold" in handled["reply"] and "call you back" in handled["reply"]
            # honest delivery: no credentials on the gateway
            assert handled["delivery"]["delivery"] == "skipped"

            # the entry LEFT the line through the callback composition (the
            # queue view shows it in the callback entries + the depth counts)
            res = await client.get(f"/voice/queues/{queue['id']}", headers=h)
            assert res.json()["depth"]["callback"] == 1
            assert res.json()["depth"]["waiting"] == 0
            entries = res.json()["callback_entries"]
            assert entries[0]["id"] == entry_id
            assert entries[0]["status"] == "callback"
            assert entries[0]["meta"]["callback"]["requested_at"]
            # the backchannel log tells the story: reply -> action -> reply sent
            events = [e["event"] for e in entries[0]["meta"]["sms"]]
            assert "inbound_reply" in events and "auto_answer_reply" in events

            # the call ended honestly (reason=callback, not hangup)
            res = await client.get(f"/voice/sessions/{session['id']}", headers=h)
            assert res.json()["state"] == "ended"
            assert res.json()["end_reason"] == "callback"
            kinds = [e["kind"] for e in res.json()["events"]]
            assert "queue.sms" in kinds  # the inbound reply + the leave story

            # the composed campaign now carries the callback target
            res = await client.get("/voice/campaigns", headers=h)
            camps = [c for c in res.json()["campaigns"]
                     if c.get("config", {}).get("callback_for_queue") == queue["id"]]
            assert len(camps) == 1
            res = await client.get(f"/voice/campaigns/{camps[0]['id']}", headers=h)
            assert res.json()["progress"]["total"] >= 1
            assert res.json()["targets"][0]["address"] == "+15550007777"

            # ---- a NON-keyword text rides the NORMAL conversation path --
            session2 = await _mk_live_session(client, h, frm="+15550008888")
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": session2["id"]})
            assert res.status_code == 201, res.text
            posted = _sms_post(ep, {"from": "+15550008888", "text": "how long?"},
                               "sms-sekrit-1")
            res = await client.post(posted["url"], content=posted["content"],
                                    headers=posted["headers"])
            assert res.status_code == 200, res.text
            out = res.json()
            handled = out["handled"][0]
            assert "auto_answer" not in handled  # not the queue's business
            assert handled.get("conversation_id")  # the conversation layer took it

            # ---- a keyword from a NON-waiting sender: normal path too ---
            posted = _sms_post(ep, {"from": "+15550009999", "text": "1"},
                               "sms-sekrit-1")
            res = await client.post(posted["url"], content=posted["content"],
                                    headers=posted["headers"])
            assert res.status_code == 200, res.text
            handled = res.json()["handled"][0]
            assert "auto_answer" not in handled
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5) abandon action + the honest refusals + config validation
# ---------------------------------------------------------------------------

def test_v79_sms_auto_answer_abandon_and_guards():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "auto2")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "line agent auto2")

            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "any-gateway auto2", "provider": "generic_sms",
                "config": {"secret": "sms-sekrit-2"}})
            assert res.status_code == 201, res.text
            ep = res.json()

            # an invalid action refuses at CONFIG WRITE (never mid-conversation)
            res = await client.post("/voice/queues", headers=h, json={
                "name": "bad action line",
                "config": {"sms": {"enabled": True, "channel_id": ep["id"],
                                   "auto_answer": {"enabled": True,
                                                   "action": "teleport"}}}})
            assert res.status_code == 400
            assert "callback|abandon" in res.json()["detail"]
            # an unknown template placeholder refuses too
            res = await client.post("/voice/queues", headers=h, json={
                "name": "bad template line",
                "config": {"sms": {"enabled": True, "channel_id": ep["id"],
                                   "auto_answer": {"enabled": True,
                                                   "reply_template": "hi {nope}"}}}})
            assert res.status_code == 400
            assert "nope" in res.json()["detail"]

            # the abandon line: reply "1" leaves the line, the call is
            # RELEASED back to in_progress (nobody is silently dropped)
            res = await client.post("/voice/queues", headers=h, json={
                "name": "abandon line", "agent_id": agent["id"],
                "config": {"sms": {"enabled": True, "channel_id": ep["id"],
                                   "auto_answer": {"enabled": True,
                                                   "action": "abandon",
                                                   "reply_template":
                                                   "{queue_name}: bye after "
                                                   "{waited_seconds}s"}}}})
            assert res.status_code == 201, res.text
            queue = res.json()

            session = await _mk_live_session(client, h, frm="+15550004321")
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": session["id"]})
            assert res.status_code == 201, res.text
            entry_id = res.json()["entry_id"]

            posted = _sms_post(ep, {"from": "+15550004321", "text": "1"},
                               "sms-sekrit-2")
            res = await client.post(posted["url"], content=posted["content"],
                                    headers=posted["headers"])
            assert res.status_code == 200, res.text
            handled = res.json()["handled"][0]
            assert handled["auto_answer"] is True
            assert handled["action"] == {"action": "abandon", "done": True,
                                         "entry_id": entry_id}
            assert "bye after" in handled["reply"]  # the custom template rendered

            res = await client.get(f"/voice/queues/{queue['id']}", headers=h)
            assert res.json()["depth"]["left"] == 1
            assert res.json()["depth"]["waiting"] == 0
            res = await client.get(f"/voice/sessions/{session['id']}", headers=h)
            assert res.json()["state"] == "in_progress"  # released, not dropped

            # ---- callback action on a queue WITHOUT callbacks: refused
            # honestly (a record, not a 500, the caller stays waiting) ----
            res = await client.post("/voice/queues", headers=h, json={
                "name": "no-cb auto line",
                "config": {"sms": {"enabled": True, "channel_id": ep["id"],
                                   "auto_answer": {"enabled": True,
                                                   "action": "callback"}}}})
            assert res.status_code == 201, res.text
            nc_queue = res.json()
            s2 = await _mk_live_session(client, h, frm="+15550005432")
            res = await client.post(f"/voice/queues/{nc_queue['id']}/entries",
                                    headers=h, json={"session_id": s2["id"]})
            assert res.status_code == 201, res.text
            nc_entry = res.json()["entry_id"]
            posted = _sms_post(ep, {"from": "+15550005432", "text": "1"},
                               "sms-sekrit-2")
            res = await client.post(posted["url"], content=posted["content"],
                                    headers=posted["headers"])
            assert res.status_code == 200, res.text  # the WEBHOOK still succeeds
            handled = res.json()["handled"][0]
            assert handled["auto_answer"] is True
            assert handled["action"]["done"] is False
            assert "does not take callbacks" in handled["action"]["detail"]
            # the refusal is recorded on the entry (the rolled-back attempt
            # leaves the refusal record, the waiting entry untouched)
            res = await client.get(f"/voice/queues/{nc_queue['id']}", headers=h)
            entry = next(e for e in res.json()["entries"] if e["id"] == nc_entry)
            assert entry["status"] == "waiting"
            assert "auto_answer_refused" in [e["event"] for e in entry["meta"]["sms"]]

            # ---- auto-answer DISABLED: "1" is just a message -------------
            res = await client.post("/voice/queues", headers=h, json={
                "name": "plain sms line",
                "config": {"sms": {"enabled": True, "channel_id": ep["id"]}}})
            assert res.status_code == 201, res.text
            p_queue = res.json()
            assert p_queue["config"]["sms"]["auto_answer"]["enabled"] is False
            s3 = await _mk_live_session(client, h, frm="+15550006543")
            res = await client.post(f"/voice/queues/{p_queue['id']}/entries",
                                    headers=h, json={"session_id": s3["id"]})
            assert res.status_code == 201, res.text
            posted = _sms_post(ep, {"from": "+15550006543", "text": "1"},
                               "sms-sekrit-2")
            res = await client.post(posted["url"], content=posted["content"],
                                    headers=posted["headers"])
            handled = res.json()["handled"][0]
            assert "auto_answer" not in handled
            assert handled.get("conversation_id")
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 6) the version pin
# ---------------------------------------------------------------------------

def test_v79_version_pin():
    from app.config import settings

    assert settings.version == "1.85.0"
