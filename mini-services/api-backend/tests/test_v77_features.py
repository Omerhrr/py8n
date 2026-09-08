"""V77 feature tests: the waiting experience on the held leg.

Queue-position ANNOUNCEMENTS over hold (TTS on the held leg - synthesized
through the registered TTS engines, delivered on the leg's live path: the
media websocket for web legs, the provider speak command for carrier legs;
every announcement recorded on the session timeline as tts.started/
tts.ended source=queue_announcement + queue.announced), the PUSH HUB that
lets the platform speak FIRST on a live media websocket (chat frames from
the room's group chat pushed to web legs the moment they are posted), and
the SMS BACKCHANNEL for waiting callers (position updates through a bound
telnyx_sms / generic_sms channel, every send an honest record).

Runs the FastAPI app in-process (httpx ASGITransport); the one websocket
scenario speaks the ASGI spec directly (the v70 _WSClient pattern) so
every DB touch stays on the test's single event loop. No network egress.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.services import executor as executor_mod
from app.services import speech_engines as engines
from app.services import voice_push

API = "http://testserver/api/v1"


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    """The suite runs >100 register/login calls inside the sliding window;
    reset the documented test switch around each scenario."""
    from app.api import _ratelimit

    _ratelimit.reset_all()
    yield
    _ratelimit.reset_all()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
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
        "email": f"v77-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v77 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_agent(client: httpx.AsyncClient, headers: dict, name: str,
                    greeting: str = "You have reached the line.") -> dict:
    res = await client.post("/voice/agents", headers=headers, json={
        "name": name, "scaffold_handler": True, "greeting_text": greeting})
    assert res.status_code == 201, res.text
    return res.json()


async def _mk_meeting(client: httpx.AsyncClient, headers: dict, agent_id: str,
                      labels: tuple[str, ...] = ()) -> tuple[dict, list[dict]]:
    res = await client.post("/voice/meetings", headers=headers,
                            json={"title": "v77 room", "agent_id": agent_id})
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
    """An answered inbound call (in_progress) - queueable."""
    res = await client.post("/voice/sessions", headers=headers, json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"cc-{frm}",
        "from_ref": frm, "to_ref": "+15550002222"})
    assert res.status_code == 201, res.text
    sid = res.json()["id"]
    res = await client.post(f"/voice/sessions/{sid}/events", headers=headers,
                            json={"kind": "call.ringing", "payload": {}})
    assert res.status_code == 200, res.text
    res = await client.post(f"/voice/sessions/{sid}/events", headers=headers,
                            json={"kind": "call.answered", "payload": {}})
    assert res.status_code == 200, res.text
    res = await client.get(f"/voice/sessions/{sid}", headers=headers)
    return res.json()


# ---------------------------------------------------------------------------
# the v70 _WSClient pattern: an ASGI websocket client on the test's own loop
# ---------------------------------------------------------------------------


class _WSClient:
    """A minimal ASGI websocket client that runs ON the test's event loop
    (starlette's TestClient would move the media handler's DB touches to a
    worker-thread loop - the exact 'database is locked' trap v70 wrote
    down)."""

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
            raise WebSocketDisconnect(code, first.get("reason", ""))
        assert first["type"] == "websocket.accept", first

    async def send_text(self, text: str) -> None:
        await self._incoming.put({"type": "websocket.receive", "text": text})

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
                raise WebSocketDisconnect(msg.get("code", 1000), msg.get("reason", ""))

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
# a deterministic TTS engine for the delivery test (real RIFF wav bytes)
# ---------------------------------------------------------------------------


def _fake_wav(text: str, voice: str = "", fmt: str = "wav") -> bytes:
    n = max(80, len(text) * 80)
    samples = struct.pack(f"<{n}h", *([900] * n))
    hdr = b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
    hdr += b"data" + struct.pack("<I", len(samples))
    return hdr + samples


class _FakeWebsocket:
    """A push-hub unit double: records frames, can die on demand."""

    def __init__(self, die_on: int | None = None):
        self.frames: list[dict] = []
        self._die_on = die_on
        self._sends = 0

    async def send_text(self, text: str) -> None:
        self._sends += 1
        if self._die_on is not None and self._sends >= self._die_on:
            raise RuntimeError("socket died mid-push")
        self.frames.append(json.loads(text))


# ---------------------------------------------------------------------------
# 1) the push hub itself (pure registry): register / push / dead sockets
# ---------------------------------------------------------------------------

def test_v77_push_hub_units():
    async def _go():
        fake_a = _FakeWebsocket()
        fake_b = _FakeWebsocket(die_on=1)
        sock_a = voice_push.register("sess-push", fake_a)
        sock_b = voice_push.register("sess-push", fake_b)
        assert voice_push.connection_count("sess-push") == 2
        assert "sess-push" in voice_push.connected_sessions()

        n = await voice_push.push("sess-push", {"event": "chat", "hello": True})
        assert n == 1  # fake_b died on its first send and was dropped
        assert voice_push.connection_count("sess-push") == 1
        assert fake_a.frames == [{"event": "chat", "hello": True}]

        # nobody home: honest zero, no error
        assert await voice_push.push("sess-ghost", {"event": "chat"}) == 0

        # many legs at once (the meeting chat's shape)
        voice_push.register("sess-push", _FakeWebsocket())
        multi = await voice_push.push_to_session_legs(
            ["sess-push", "sess-push", "", None, "sess-other"],
            {"event": "queue_position", "position": 3})
        assert multi == {"legs": 1, "delivered": 2}

        voice_push.unregister("sess-push", sock_a)
        assert voice_push.connection_count("sess-push") == 1
        voice_push.unregister("sess-push", sock_b)  # already gone - fine

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2) queue-position announcements over hold (TTS on the held leg)
# ---------------------------------------------------------------------------

def test_v77_queue_position_announcements():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "ann")
            h = _auth(user["token"])
            await _mk_agent(client, h, "line agent")

            # a bad template refuses at CONFIG WRITE, never mid-announcement
            res = await client.post("/voice/queues", headers=h, json={
                "name": "broken template line",
                "config": {"announce": {"template": "you are {bogus}"}}})
            assert res.status_code == 400
            assert "bogus" in res.json()["detail"]

            # a clean TTS slate for the pass logic: whatever earlier tests
            # (the real piper bridge) bound is remembered and restored after
            was_registered = engines.unregister_tts_engine(engines.LOCAL_TTS_NAME)
            try:
                res = await client.post("/voice/queues", headers=h, json={
                    "name": "support line",
                    "config": {"max_wait_seconds": 600,
                               "announce": {"template": "Position {position} of "
                                                          "{depth} in {queue_name}, "
                                                          "waited {waited_seconds}s."}}})
                assert res.status_code == 201, res.text
                queue = res.json()
                assert queue["config"]["announce"]["enabled"] is True
                assert queue["config"]["announce"]["interval_seconds"] == 60
                # SMS backchannel defaults OFF (opt-in)
                assert queue["config"]["sms"]["enabled"] is False

                session = await _mk_live_session(client, h, frm="+15550007777")
                res = await client.post(
                    f"/voice/queues/{queue['id']}/entries", headers=h,
                    json={"session_id": session["id"]})
                assert res.status_code == 201, res.text
                joined = res.json()
                entry_id = joined["entry_id"]
                # the join pass RAN (force=joined): the position was computed,
                # synthesized-NO, and recorded honestly - the caller is on a
                # carrier leg with no speak endpoint, the record says exactly that
                ann = joined["announcement"]["missed"]
                assert len(ann) == 1 and ann[0]["entry_id"] == entry_id
                assert ann[0]["position"] == 1
                assert ann[0]["delivery"]["delivery"] == "skipped"
                # no engine bound yet: the position was computed but not spoken
                assert "no TTS engine" in ann[0]["delivery"]["detail"]

                # the session timeline keeps the whole story
                res = await client.get(f"/voice/sessions/{session['id']}", headers=h)
                kinds = [e["kind"] for e in res.json()["events"] or []]
                assert "queue.announced" in kinds
                # no engine -> no tts.started, only the queue.announced record
                assert "tts.started" not in kinds

                # an immediate pass is NOT due (position unchanged, interval fresh)
                res = await client.post(f"/voice/queues/{queue['id']}/announce",
                                        headers=h, json={"force": False})
                assert res.status_code == 200, res.text
                assert res.json()["announced"][0]["reason"] == "not_due"

                # forced (the button): re-announced - the meta counts it
                res = await client.post(f"/voice/queues/{queue['id']}/announce",
                                        headers=h, json={"force": True})
                assert res.status_code == 200, res.text
                assert res.json()["announced"][0]["announced"] is False  # still no engine

                # an engine bound but a CARRIER leg with no speak endpoint:
                # the speak path is the honest skip (recorded, never invented)
                engines.register_tts_engine(engines.LOCAL_TTS_NAME, _fake_wav)
                res = await client.post(f"/voice/queues/{queue['id']}/announce",
                                        headers=h, json={"force": True})
                assert res.status_code == 200, res.text
                carrier_try = res.json()["announced"][0]
                assert carrier_try["announced"] is False
                assert "telnyx" in carrier_try["delivery"]["detail"]

                # ---- the REAL delivery: a web leg on the media websocket ----
                try:
                    ws = _WSClient(app, f"/api/v1/voice/sessions/{session['id']}/media",
                                   token=user["token"])
                    await ws.connect()
                    try:
                        # the connect handshake itself pushes the connected frame
                        hello = await ws.receive_json()
                        assert hello["event"] == "connected"
                        res = await client.post(
                            f"/voice/queues/{queue['id']}/announce",
                            headers=h, json={"force": True})
                        assert res.status_code == 200, res.text
                        out = res.json()["announced"][0]
                        assert out["announced"] is True
                        assert out["delivery"]["path"] == "web_media_socket"
                        assert out["delivery"]["frames"] == 2  # position + audio

                        pos_frame = await ws.receive_json()
                        assert pos_frame["event"] == "queue_position"
                        assert pos_frame["queue_id"] == queue["id"]
                        assert pos_frame["position"] == 1 and pos_frame["depth"] == 1
                        assert "Position 1 of 1" in pos_frame["text"]
                        audio_frame = await ws.receive_json()
                        assert audio_frame["event"] == "audio"
                        assert audio_frame["source"] == "queue_announcement"
                        assert base64.b64decode(audio_frame["audio_b64"])[:4] == b"RIFF"
                    finally:
                        await ws.close()
                finally:
                    engines.unregister_tts_engine(engines.LOCAL_TTS_NAME)
                if was_registered:
                    engines.bind_local_engines()  # restore the real bridge

                # with an engine bound, the announcement records its TTS events
                res = await client.get(f"/voice/sessions/{session['id']}", headers=h)
                events = res.json()["events"] or []
                kinds = [e["kind"] for e in events]
                assert "tts.started" in kinds and "tts.ended" in kinds
                announced_ev = [e for e in events if e["kind"] == "queue.announced"]
                assert announced_ev, "the queue.announced records are the announcement log"
                last = announced_ev[-1]["payload"]
                assert last["source"] == "queue_announcement"
                assert last["position"] == 1 and last["depth"] == 1
                assert last["delivery"]["delivery"] == "delivered"
                assert last["delivery"]["path"] == "web_media_socket"
                # the entry's meta keeps the operational counters
                res = await client.get(f"/voice/queues/{queue['id']}", headers=h)
                meta = res.json()["entries"][0]["meta"]
                assert meta["last_announced_position"] == 1
                assert meta["announcements"] == 4  # join + forced + carrier + ws

            finally:
                engines.unregister_tts_engine(engines.LOCAL_TTS_NAME)

            # a queue with announcements disabled refuses the pass loudly
            res = await client.post("/voice/queues", headers=h, json={
                "name": "silent line", "config": {"announce": {"enabled": False}}})
            assert res.status_code == 201, res.text
            silent_q = res.json()
            res = await client.post(f"/voice/queues/{silent_q['id']}/announce",
                                    headers=h, json={"force": True})
            assert res.status_code == 400
            assert "disabled" in res.json()["detail"]

            # unknown queue -> the honest not-found refusal (400-shaped, exact reason)
            res = await client.post("/voice/queues/q-nope/announce", headers=h,
                                    json={"force": True})
            assert res.status_code == 400
            assert "not found" in res.json()["detail"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3) room chat pushed to web legs via the media websocket
# ---------------------------------------------------------------------------

def test_v77_chat_pushed_to_web_legs():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "chatpush")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "room persona")
            meeting, legs = await _mk_meeting(client, h, agent["id"],
                                              labels=("ada web", "grace web"))
            leg_a, leg_b = legs
            assert leg_a["session_id"] and leg_b["session_id"]

            # a web leg listening on its media websocket (registered with the
            # push hub the moment the socket is accepted)
            ws = _WSClient(app, f"/api/v1/voice/sessions/{leg_a['session_id']}/media",
                           token=user["token"])
            await ws.connect()
            try:
                hello = await ws.receive_json()
                assert hello["event"] == "connected"

                # the moderator posts: the frame reaches the live leg NOW
                res = await client.post(f"/voice/meetings/{meeting['id']}/chat",
                                        headers=h,
                                        json={"text": "support engineer joining in a moment"})
                assert res.status_code == 200, res.text
                out = res.json()
                assert out["push"]["legs"] == 1 and out["push"]["delivered"] == 1
                frame = await ws.receive_json()
                assert frame["event"] == "chat"
                assert frame["meeting_id"] == meeting["id"]
                assert frame["message"]["role"] == "moderator"
                assert frame["message"]["text"] == "support engineer joining in a moment"

                # a member asks the agent from THEIR leg: the reply is pushed
                # to every live leg too (the room hears the answer)
                res = await client.post(f"/voice/meetings/{meeting['id']}/chat",
                                        headers=h,
                                        json={"text": "what is the return window?",
                                              "participant_id": leg_b["id"],
                                              "ask_agent": True})
                assert res.status_code == 200, res.text
                out = res.json()
                assert "agent_reply" in out
                # the asking member's own message pushes first, then the reply
                ask_frame = await ws.receive_json()
                assert ask_frame["event"] == "chat"
                assert ask_frame["message"]["role"] == "member"
                reply_frame = await ws.receive_json()
                assert reply_frame["event"] == "chat"
                assert reply_frame["message"]["role"] == "agent"
                assert reply_frame["message"]["meta"].get("in_reply_to")

                # the log is the source of truth (the push is best-effort)
                res = await client.get(f"/voice/meetings/{meeting['id']}/chat",
                                       headers=h)
                roles = [m["role"] for m in res.json()["messages"]]
                assert roles.count("agent") >= 1

                # no live sockets -> the chat still lands, the push is honest zero
                await ws.close()
                res = await client.post(f"/voice/meetings/{meeting['id']}/chat",
                                        headers=h, json={"text": "anyone there?"})
                assert res.status_code == 200, res.text
                assert res.json()["push"]["legs"] == 0
                assert res.json()["push"]["delivered"] == 0
            finally:
                await ws.close()

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4) the SMS backchannel for waiting callers
# ---------------------------------------------------------------------------

def test_v77_sms_backchannel():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "sms")
            h = _auth(user["token"])
            await _mk_agent(client, h, "line agent")

            # a generic_sms channel WITHOUT outbound credentials: sends are
            # honest skipped records (the any-gateway contract)
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "any-gateway", "provider": "generic_sms",
                "config": {"secret": "sms-sekrit"}})
            assert res.status_code == 201, res.text
            sms_channel = res.json()

            # opt-in but unbound: the queue is created, the pass refuses loudly
            res = await client.post("/voice/queues", headers=h, json={
                "name": "sms line", "config": {"sms": {"enabled": True}}})
            assert res.status_code == 201, res.text
            unbound_q = res.json()
            res = await client.post(f"/voice/queues/{unbound_q['id']}/sms-update",
                                    headers=h, json={"event": "position"})
            assert res.status_code == 400
            assert "channel_id" in res.json()["detail"]

            # wrong-provider binding refuses at the pass with the provider list
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "inbox", "provider": "email_inbound",
                "config": {"secret": "mail-sekrit"}})
            assert res.status_code == 201, res.text
            email_channel = res.json()
            res = await client.post("/voice/queues", headers=h, json={
                "name": "wrong line",
                "config": {"sms": {"enabled": True, "channel_id": email_channel["id"]}}})
            assert res.status_code == 201, res.text
            res = await client.post(f"/voice/queues/{res.json()['id']}/sms-update",
                                    headers=h, json={})
            assert res.status_code == 400
            assert "telnyx_sms" in res.json()["detail"]
            assert "generic_sms" in res.json()["detail"]

            # unknown channel id refuses with the exact reason
            res = await client.post("/voice/queues", headers=h, json={
                "name": "ghost line",
                "config": {"sms": {"enabled": True, "channel_id": "ch-nope"}}})
            assert res.status_code == 201, res.text
            ghost_q = res.json()
            res = await client.post(f"/voice/queues/{ghost_q['id']}/sms-update",
                                    headers=h, json={})
            assert res.status_code == 400
            assert "not found" in res.json()["detail"]

            # unknown event name refuses
            res = await client.post("/voice/queues", headers=h, json={
                "name": "bound line",
                "config": {"sms": {"enabled": True, "channel_id": sms_channel["id"]}}})
            assert res.status_code == 201, res.text
            queue = res.json()
            assert queue["config"]["sms"]["enabled"] is True
            res = await client.post(f"/voice/queues/{queue['id']}/sms-update",
                                    headers=h, json={"event": "bogus"})
            assert res.status_code == 400
            assert "position" in res.json()["detail"]

            # a queue without SMS enabled refuses too
            res = await client.post("/voice/queues", headers=h,
                                    json={"name": "plain line"})
            assert res.status_code == 201, res.text
            res = await client.post(f"/voice/queues/{res.json()['id']}/sms-update",
                                    headers=h, json={})
            assert res.status_code == 400
            assert "not enabled" in res.json()["detail"]

            # ---- the real pass: a waiting caller is TEXTED --------------
            session = await _mk_live_session(client, h, frm="+15550004444")
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": session["id"]})
            assert res.status_code == 201, res.text
            joined = res.json()
            entry_id = joined["entry_id"]
            # the JOIN ran the sms pass (event=joined): no credentials, so the
            # send is a skipped record - recorded, not silent
            sent = joined["sms"]["sent"]
            assert len(sent) == 1 and sent[0]["entry_id"] == entry_id
            assert sent[0]["delivery"] == "skipped"
            assert "send_url" in sent[0]["detail"]
            assert "+15550004444" in sent[0]["to"]
            assert "#1 of 1" in sent[0]["text"]

            # the record lives on the entry AND the session timeline
            res = await client.get(f"/voice/queues/{queue['id']}", headers=h)
            meta = res.json()["entries"][0]["meta"]
            assert len(meta["sms"]) == 1 and meta["sms"][0]["event"] == "joined"
            res = await client.get(f"/voice/sessions/{session['id']}", headers=h)
            kinds = [e["kind"] for e in res.json()["events"]]
            assert "queue.sms" in kinds

            # an explicit position pass (the operator button / the scheduled job)
            res = await client.post(f"/voice/queues/{queue['id']}/sms-update",
                                    headers=h, json={"event": "position"})
            assert res.status_code == 200, res.text
            assert res.json()["event"] == "position"
            res = await client.get(f"/voice/queues/{queue['id']}", headers=h)
            assert len(res.json()["entries"][0]["meta"]["sms"]) == 2

            # a gateway WITH credentials but unreachable: the failure is a
            # record too (loopback port 9 - refused instantly, no egress)
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "dead gateway", "provider": "generic_sms",
                "config": {"secret": "s", "send_url": "http://127.0.0.1:9/send",
                           "bearer_token": "bt", "from_number": "PY8N"}})
            assert res.status_code == 201, res.text
            dead_channel = res.json()
            res = await client.post("/voice/queues", headers=h, json={
                "name": "dead line",
                "config": {"sms": {"enabled": True, "channel_id": dead_channel["id"]}}})
            assert res.status_code == 201, res.text
            dead_q = res.json()
            res = await client.post(f"/voice/queues/{dead_q['id']}/entries",
                                    headers=h, json={"session_id": session["id"]})
            # the session is already waiting in the other queue - duplicate refused
            assert res.status_code == 400
            session2 = await _mk_live_session(client, h, frm="+15550005555")
            res = await client.post(f"/voice/queues/{dead_q['id']}/entries",
                                    headers=h, json={"session_id": session2["id"]})
            assert res.status_code == 201, res.text
            sent = res.json()["sms"]["sent"]
            assert len(sent) == 1 and sent[0]["delivery"] == "failed"
            assert "127.0.0.1" in sent[0]["detail"] or "failed" in sent[0]["detail"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5) platform pins
# ---------------------------------------------------------------------------

def test_v77_version_pin():
    from app.config import settings

    # relaxed to >= when the next batch pinned the new version
    assert tuple(int(p) for p in settings.version.split(".")) >= (1, 78, 0)
