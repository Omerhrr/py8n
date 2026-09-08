"""V78 feature tests: callbacks instead of hold, the measured line/room,
the support-line-system solution, and first-class video.

* CALLBACKS (queue -> campaign composition): a waiting caller trades the
  hold for a callback (the call ends honestly with reason=callback, a
  target is booked on the queue's COMPOSED campaign, the entry keeps its
  joined_at); dial_callbacks runs the campaign's own start pass (injected
  sender); an ANSWERED callback walks into the queue's destination
  meeting - through the real on_call_event path AND the simulate path.
* ANALYTICS on the new events: queue_analytics (per-outcome waits,
  abandonment rate, announcement/SMS delivery splits, callback picture)
  and meeting_analytics (chat by role, raise->floor waits, per-leg
  confidence) - derived at read time, nothing stored.
* SUPPORT LINE SYSTEM: the second voice solution installs the agent +
  the room + the queue pre-wired (announce + SMS + callback blocks).
* VIDEO: the track registry (publish/unpublish with video.started/
  stopped on the leg's timeline), the push hub telling the other legs,
  and the WebRTC signaling relay over the live media websockets (the
  v70 _WSClient ASGI pattern) - py8n relays the handshake, never the
  pixels.

Runs the FastAPI app in-process (httpx ASGITransport); the websocket
scenarios speak the ASGI spec directly so every DB touch stays on the
test's single event loop. No network egress.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401 - parity with v77

from app.main import app
from app.db import AsyncSessionLocal
from app.services import executor as executor_mod
from app.services import speech_engines as engines

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
        "email": f"v78-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v78 u{n} {tag}",
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
                            json={"title": "v78 room", "agent_id": agent_id})
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
    for kind in ("call.ringing", "call.answered"):
        res = await client.post(f"/voice/sessions/{sid}/events", headers=headers,
                                json={"kind": kind, "payload": {}})
        assert res.status_code == 200, res.text
    res = await client.get(f"/voice/sessions/{sid}", headers=headers)
    return res.json()


# the v70 _WSClient pattern: an ASGI websocket client on the test's own loop
# (verbatim from v77 - the 'database is locked' trap keeps its warning)

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
# 1) callbacks instead of hold - the queue composes with the dialer
# ---------------------------------------------------------------------------

def _cb_sender(book: list[str]):
    """The injected dial sender: every dial 'succeeds' with a fresh id."""
    async def _sender(cfg, req):
        cid = f"cc-cb-{len(book):03d}"
        book.append(cid)
        return {"status_code": 200, "json": {"data": {"call_control_id": cid}},
                "note": req["json"].get("client_state", "")[:8]}
    return _sender


def test_v78_callbacks_instead_of_hold():
    async def _go():
        from app.models import VoiceCampaign, VoiceCampaignTarget, VoiceSession
        from app.services import voice_callbacks as cb_svc
        from app.services import voice_campaigns as camp_svc

        async with _client() as client:
            user = await _mk_user(client, "cb")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "line agent cb")
            # the destination room the answered callbacks walk into
            meeting, _legs = await _mk_meeting(client, h, agent["id"])
            # the dialing endpoint (credentials the campaign dials through)
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "cb telnyx", "provider": "telnyx_call_control",
                "config": {"api_key": "telnyx-key-cb",
                           "public_key": "-----BEGIN PUBLIC KEY-----\nMFow\n-----END PUBLIC KEY-----",
                           "connection_id": "conn-cb",
                           "webhook_url": "https://py8n.example/api/v1/channels/telnyx/x/webhook",
                           "from_number": "+15559997777"}})
            assert res.status_code in (200, 201), res.text
            ep = res.json()

            # a disabled queue refuses callbacks loudly
            res = await client.post("/voice/queues", headers=h, json={
                "name": "no callbacks line", "agent_id": agent["id"],
                "config": {"callback": {"enabled": False}}})
            assert res.status_code == 201
            plain_q = res.json()
            assert plain_q["config"]["callback"]["enabled"] is False
            s0 = await _mk_live_session(client, h, frm="+15550004000")
            res = await client.post(f"/voice/queues/{plain_q['id']}/entries",
                                    headers=h, json={"session_id": s0["id"]})
            assert res.status_code == 201
            res = await client.post(f"/voice/queues/{plain_q['id']}/callbacks",
                                    headers=h, json={"entry_id": res.json()["entry_id"]})
            assert res.status_code == 400
            assert "does not take callbacks" in res.json()["detail"]

            # the real line: callback enabled, endpoint bound, room bound
            res = await client.post("/voice/queues", headers=h, json={
                "name": "callbacks line", "agent_id": agent["id"],
                "meeting_id": meeting["id"],
                "config": {"callback": {"enabled": True, "endpoint_id": ep["id"]}}})
            assert res.status_code == 201, res.text
            queue = res.json()
            assert queue["config"]["callback"]["enabled"] is True
            assert queue["config"]["callback"]["endpoint_id"] == ep["id"]

            # two callers wait (A first, B second)
            sa = await _mk_live_session(client, h, frm="+15550004111")
            sb = await _mk_live_session(client, h, frm="+15550004222")
            entries = []
            for s in (sa, sb):
                res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                        headers=h, json={"session_id": s["id"]})
                assert res.status_code == 201, res.text
                entries.append(res.json()["entry_id"])

            # dialing with NO callbacks requested refuses honestly
            res = await client.post(f"/voice/queues/{queue['id']}/callbacks/dial",
                                    headers=h, json={})
            assert res.status_code == 400
            assert "no callbacks have been requested" in res.json()["detail"]

            # B trades the hold first, then A: the REQUEST order books the
            # dial order; each hold ends honestly (reason=callback)
            requested = []
            for entry_id in (entries[1], entries[0]):
                res = await client.post(f"/voice/queues/{queue['id']}/callbacks",
                                        headers=h, json={"entry_id": entry_id})
                assert res.status_code == 200, res.text
                out = res.json()
                assert out["campaign_id"] and out["target_id"]
                requested.append(out)
                # the v77 passes ran: the line moved for the remaining waiter
                assert "announcement" in out
            # the holds ended with the honest reason
            for s in (sa, sb):
                res = await client.get(f"/voice/sessions/{s['id']}", headers=h)
                body = res.json()
                assert body["state"] == "ended"
                assert body["end_reason"] == "callback"
            # the queue keeps its place: two callback entries, nobody waiting
            res = await client.get(f"/voice/queues/{queue['id']}", headers=h)
            qd = res.json()
            assert qd["depth"]["callback"] == 2
            assert qd["depth"]["waiting"] == 0
            assert qd["callbacks"]["requested"] == 2
            assert qd["callbacks"]["campaign_id"]
            # the composed campaign is a real campaign carrying the marker
            res = await client.get(f"/voice/campaigns/{qd['callbacks']['campaign_id']}",
                                   headers=h)
            assert res.status_code == 200
            camp = res.json()
            assert camp["config"]["callback_for_queue"] == queue["id"]
            assert camp["agent_id"] == agent["id"]
            assert camp["name"].startswith("callback:")

            # the dial pass: in REQUEST order (B first), through the injected
            # sender (the wire the campaign always uses)
            book: list[str] = []
            async with AsyncSessionLocal() as db:
                out = await cb_svc.dial_callbacks(db, user["id"], queue["id"],
                                                  sender=_cb_sender(book))
                await db.commit()
            assert out["start_note"] == "2 dial(s) placed"
            assert book == [f"cc-cb-{i:03d}" for i in range(2)]
            # read the targets back through the API (dialing + booked order)
            res = await client.get(f"/voice/campaigns/{camp['id']}", headers=h)
            targets = res.json()["targets"]
            assert all(t["status"] == "dialing" for t in targets)
            assert targets[0]["call_control_id"] == book[0]
            assert targets[0]["address"] == "+15550004222"  # B requested first
            assert targets[1]["address"] == "+15550004111"

            # the ANSWERED callback walks into the room (the REAL path: the
            # carrier's call.answered webhook -> on_call_event -> the hook)
            async with AsyncSessionLocal() as db:
                link = await camp_svc.on_call_event(
                    db, call_control_id=book[0], event_kind="call.answered")
                await db.commit()
            assert link is not None and link["status"] == "answered"
            cb_session_id = link["session_id"]
            res = await client.get(f"/voice/meetings/{meeting['id']}", headers=h)
            md = res.json()
            joined = [p for p in md["participants"] if p["session_id"] == cb_session_id]
            assert len(joined) == 1 and joined[0]["state"] == "joined"
            assert joined[0]["address"] == "+15550004222"
            # the entry books the answer
            res = await client.get(f"/voice/queues/{queue['id']}", headers=h)
            cbe = {e["id"]: e for e in res.json()["callback_entries"]}
            answered_entry = [e for e in res.json()["callback_entries"]
                              if (e["meta"].get("callback") or {}).get("answered_at")]
            assert len(answered_entry) == 1
            assert answered_entry[0]["meta"]["callback"]["attached"]["meeting_id"] == meeting["id"]

            # the SIMULATE path books the same hook
            res = await client.post(
                f"/voice/campaigns/{camp['id']}/targets/{targets[1]['id']}/simulate-answer",
                headers=h, json={})
            assert res.status_code == 200, res.text
            sim = res.json()
            assert sim["callback"]["simulated"] is True
            assert sim["callback"]["attached"]["meeting_id"] == meeting["id"]

            # a stranger's queue is nobody's line
            other = await _mk_user(client, "cb", n=2)
            res = await client.get(f"/voice/queues/{queue['id']}",
                                   headers=_auth(other["token"]))
            assert res.status_code == 404

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2) the line, measured - queue analytics on the new events
# ---------------------------------------------------------------------------

def test_v78_queue_analytics():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "qa")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "measured line")

            # keep the TTS slate clean (the real piper bridge may be bound);
            # the announcement pass then records honest SKIPPED deliveries
            was_registered = engines.unregister_tts_engine(engines.LOCAL_TTS_NAME)
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "qa sms gateway", "provider": "generic_sms",
                "config": {"secret": "qa-sms-sekrit"}})
            assert res.status_code in (200, 201), res.text
            sms_ep = res.json()
            try:
                res = await client.post("/voice/queues", headers=h, json={
                    "name": "measured line",
                    "agent_id": agent["id"],
                    "config": {"max_wait_seconds": 600,
                               "callback": {"enabled": True},
                               "sms": {"enabled": True, "channel_id": sms_ep["id"]}}})
                assert res.status_code == 201, res.text
                queue = res.json()

                # A: waits and gets SEATED (no meeting bound - released only)
                sa = await _mk_live_session(client, h, frm="+15550005111")
                res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                        headers=h, json={"session_id": sa["id"]})
                entry_a = res.json()["entry_id"]
                res = await client.post(f"/voice/queues/{queue['id']}/next", headers=h)
                assert res.status_code == 200, res.text
                # B: waits and is taken OUT (left)
                sb = await _mk_live_session(client, h, frm="+15550005222")
                res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                        headers=h, json={"session_id": sb["id"]})
                entry_b = res.json()["entry_id"]
                res = await client.post(
                    f"/voice/queues/{queue['id']}/entries/{entry_b}/leave", headers=h)
                assert res.status_code == 200
                # C: hangs up while waiting (derived abandoned)
                sc = await _mk_live_session(client, h, frm="+15550005333")
                res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                        headers=h, json={"session_id": sc["id"]})
                res = await client.post(f"/voice/sessions/{sc['id']}/events",
                                        headers=h, json={"kind": "hangup",
                                                         "payload": {}})
                assert res.status_code == 200
                # D: trades the hold for a callback
                sd = await _mk_live_session(client, h, frm="+15550005444")
                res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                        headers=h, json={"session_id": sd["id"]})
                entry_d = res.json()["entry_id"]
                res = await client.post(f"/voice/queues/{queue['id']}/callbacks",
                                        headers=h, json={"entry_id": entry_d})
                assert res.status_code == 200, res.text
                # E: still waiting (the live line)
                se = await _mk_live_session(client, h, frm="+15550005555")
                res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                        headers=h, json={"session_id": se["id"]})
                assert res.status_code == 201

                # an explicit SMS pass (skipped honestly: the gateway has no
                # credentials) and a forced announcement pass
                res = await client.post(f"/voice/queues/{queue['id']}/sms-update",
                                        headers=h, json={"event": "position"})
                assert res.status_code == 200, res.text
                sms_out = res.json()
                assert sms_out["sent"], "the waiting callers were considered"
                res = await client.post(f"/voice/queues/{queue['id']}/announce",
                                        headers=h, json={"force": True})
                assert res.status_code == 200, res.text

                res = await client.get(f"/voice/queues/{queue['id']}/analytics",
                                       headers=h)
                assert res.status_code == 200, res.text
                an = res.json()
                assert an["queue_id"] == queue["id"]
                assert an["entries_scanned"] == 5
                # the outcome picture, exactly as the line lived it (the
                # abandoned caller KEEPS the waiting status - abandonment
                # is DERIVED from the session state, never a status)
                oc = an["outcomes"]
                assert oc["seated"] == 1 and oc["left"] == 1
                assert oc["callback"] == 1 and oc["waiting"] == 2
                # abandonment is DERIVED (session ended while waiting)
                assert an["abandonment"]["abandoned"] == 1
                assert an["abandonment"]["ever_waiting"] == 5
                assert an["abandonment"]["rate"] == 0.2
                # waits: closed entries (seated + left + the traded callback)
                # and one abandoned; one still waiting now
                assert an["waits"]["closed"]["count"] == 3
                assert an["waits"]["abandoned"]["count"] == 1
                assert an["waits"]["waiting_now"]["count"] == 1
                assert an["waits"]["sla_seconds"] == 600
                # announcements: several passes ran (join + moves + forced);
                # every delivery was an honest SKIP (no TTS engine bound)
                assert an["announcements"]["total"] >= 1
                assert an["announcements"]["delivery_split"].get("skipped", 0) \
                    == an["announcements"]["total"]
                # sms: the explicit pass recorded its skip
                assert an["sms"]["enabled"] is True
                assert an["sms"]["delivery_split"].get("skipped", 0) \
                    == an["sms"]["total"] >= 1
                # the callback picture rides the analytics
                assert an["callbacks"]["requested"] == 1
            finally:
                if was_registered:
                    engines.register_tts_engine(engines.LOCAL_TTS_NAME,
                                                was_registered)

            # a stranger's 404 is a 404, not a leak
            other = await _mk_user(client, "qa", n=2)
            res = await client.get(f"/voice/queues/{queue['id']}/analytics",
                                   headers=_auth(other["token"]))
            assert res.status_code == 404

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3) the room, measured - meeting analytics on the new events
# ---------------------------------------------------------------------------

def test_v78_meeting_analytics():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "ma")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "measured room persona")
            meeting, legs = await _mk_meeting(client, h, agent["id"],
                                              ("Alice", "Bob"))
            alice, bob = legs
            assert alice["session_id"] and bob["session_id"]

            # the text side channel: a member line, a moderator line, and
            # an ask-the-agent reply
            res = await client.post(f"/voice/meetings/{meeting['id']}/chat",
                                    headers=h,
                                    json={"text": "what are your hours?",
                                          "participant_id": alice["id"]})
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/meetings/{meeting['id']}/chat",
                                    headers=h,
                                    json={"text": "welcome everyone", "author": "Mo"})
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/meetings/{meeting['id']}/chat",
                                    headers=h,
                                    json={"text": "say the welcome line",
                                          "participant_id": alice["id"],
                                          "ask_agent": True})
            assert res.status_code == 200, res.text
            assert res.json().get("agent_reply")

            # the speaking queue: Alice raises, gets the floor (granted);
            # Bob raises, is lowered (lowered) - both land in the history
            res = await client.post(f"/voice/meetings/{meeting['id']}/hand",
                                    headers=h, json={"participant_id": alice["id"],
                                                     "note": "next question"})
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/meetings/{meeting['id']}/hand/next",
                                    headers=h)
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/meetings/{meeting['id']}/hand",
                                    headers=h, json={"participant_id": bob["id"]})
            assert res.status_code == 200, res.text
            res = await client.delete(
                f"/voice/meetings/{meeting['id']}/hand/{bob['id']}", headers=h)
            assert res.status_code == 200, res.text

            # the conversation: two spoken turns on Alice's leg (reported
            # confidence) and one agent reply already recorded via chat
            for text, conf in (("what are your hours", 0.91),
                               ("thanks that answers it", 0.87)):
                res = await client.post(f"/voice/sessions/{alice['session_id']}/turn",
                                        headers=h,
                                        json={"transcript": text,
                                              "confidence": conf})
                assert res.status_code == 200, res.text

            res = await client.get(f"/voice/meetings/{meeting['id']}/analytics",
                                   headers=h)
            assert res.status_code == 200, res.text
            an = res.json()
            assert an["meeting_id"] == meeting["id"]
            assert an["legs"]["by_channel"] == {"web": 2}
            assert an["legs"]["by_state"]["joined"] == 2
            # chat by role, with the agent's reply counted (the ask_agent
            # post is itself a member message + the agent's reply row)
            assert an["chat"]["total"] == 4
            assert an["chat"]["by_role"]["member"] == 2
            assert an["chat"]["by_role"]["moderator"] == 1
            assert an["chat"]["agent_replies"] == 1
            assert an["chat"]["ask_agent"] == 1
            # the raise -> outcome record with the waits
            sq = an["speaking_queue"]
            assert sq["granted_floor"] == 1 and sq["lowered"] == 1
            assert sq["resolved_total"] == 2
            assert sq["waiting_now"] == 0
            assert sq["raise_to_floor_seconds"]["count"] == 1
            assert sq["raise_to_floor_seconds"]["mean_seconds"] is not None
            # the per-leg conversation picture: Alice said two lines
            per_leg = {p["label"]: p for p in an["conversation"]["per_leg"]}
            assert per_leg["Alice"]["participant_lines"] == 2
            assert per_leg["Alice"]["confidence"]["turns_reported"] == 2
            assert per_leg["Alice"]["confidence"]["mean"] == pytest.approx(0.89, abs=0.01)
            assert an["conversation"]["participant_lines"] == 2
            # the agent spoke to the asking leg (tts.started on Alice's leg)
            assert per_leg["Alice"]["agent_lines"] >= 1
            assert an["conversation"]["agent_lines"] >= 1

            other = await _mk_user(client, "ma", n=2)
            res = await client.get(f"/voice/meetings/{meeting['id']}/analytics",
                                   headers=_auth(other["token"]))
            assert res.status_code == 404

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4) first-class video - tracks, pushes, and the signaling relay
# ---------------------------------------------------------------------------

def test_v78_video_tracks_and_signaling():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "vid")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "room persona vid")
            meeting, legs = await _mk_meeting(client, h, agent["id"],
                                              ("Alice", "Bob"))
            alice, bob = legs

            # a ghost / a bad kind / a foreign room refuse loudly
            res = await client.post(f"/voice/meetings/{meeting['id']}/video/publish",
                                    headers=h, json={"participant_id": alice["id"],
                                                     "track_id": "", "kind": "camera"})
            assert res.status_code == 422  # pydantic: min_length
            assert "track_id" in json.dumps(res.json()["detail"])
            res = await client.post(f"/voice/meetings/{meeting['id']}/video/publish",
                                    headers=h, json={"participant_id": alice["id"],
                                                     "track_id": "   ",
                                                     "kind": "camera"})
            assert res.status_code == 400 and "track_id is required" in res.json()["detail"]
            res = await client.post(f"/voice/meetings/{meeting['id']}/video/publish",
                                    headers=h, json={"participant_id": alice["id"],
                                                     "track_id": "cam-1",
                                                     "kind": "hologram"})
            assert res.status_code == 400 and "hologram" in res.json()["detail"]
            res = await client.post(f"/voice/meetings/{meeting['id']}/video/publish",
                                    headers=h, json={"participant_id": "ghost",
                                                     "track_id": "cam-1",
                                                     "kind": "camera"})
            assert res.status_code == 400

            # Alice publishes her camera: the registry, the timeline event,
            # an honest zero-push (nobody has a live socket yet)
            res = await client.post(f"/voice/meetings/{meeting['id']}/video/publish",
                                    headers=h, json={"participant_id": alice["id"],
                                                     "track_id": "cam-1",
                                                     "kind": "camera"})
            assert res.status_code == 200, res.text
            pub = res.json()
            assert pub["track"]["track_id"] == "cam-1"
            assert pub["push"]["delivered"] == 0
            assert pub["push"]["notified_legs"] == 1  # Bob was CONSIDERED
            assert pub["video"]["counts"]["live_tracks"] == 1
            assert pub["video"]["counts"]["camera"] == 1

            # the duplicate refuses loudly (unpublish first)
            res = await client.post(f"/voice/meetings/{meeting['id']}/video/publish",
                                    headers=h, json={"participant_id": alice["id"],
                                                     "track_id": "cam-1",
                                                     "kind": "camera"})
            assert res.status_code == 400 and "already live" in res.json()["detail"]

            # the video.started record on Alice's leg timeline
            res = await client.get(f"/voice/sessions/{alice['session_id']}", headers=h)
            kinds = [e["kind"] for e in res.json()["events"] or []]
            assert "video.started" in kinds

            # Bob's live socket learns the news the moment it happens
            try:
                ws_b = _WSClient(app,
                                 f"/api/v1/voice/sessions/{bob['session_id']}/media",
                                 token=user["token"])
                await ws_b.connect()
                hello = await ws_b.receive_json()
                assert hello["event"] == "connected"

                # Alice shares her screen: Bob's socket gets the push
                res = await client.post(
                    f"/voice/meetings/{meeting['id']}/video/publish", headers=h,
                    json={"participant_id": alice["id"], "track_id": "screen-1",
                          "kind": "screen", "label": "quarterly deck"})
                assert res.status_code == 200, res.text
                assert res.json()["push"]["delivered"] == 1
                frame = await ws_b.receive_json()
                assert frame["event"] == "video_track"
                assert frame["action"] == "published"
                assert frame["track_id"] == "screen-1" and frame["kind"] == "screen"

                # the room's video state: the screen holder is Alice
                res = await client.get(f"/voice/meetings/{meeting['id']}/video",
                                       headers=h)
                assert res.status_code == 200, res.text
                vs = res.json()["video"]
                assert vs["counts"]["live_tracks"] == 2
                assert vs["screen_holders"] == ["Alice"]
                assert vs["counts"]["legs_with_video"] == 1

                # signaling over HTTP: A -> B, one ICE candidate, delivered 1
                res = await client.post(
                    f"/voice/meetings/{meeting['id']}/video/signal", headers=h,
                    json={"from_participant_id": alice["id"],
                          "to_participant_id": bob["id"],
                          "data": {"type": "offer", "sdp": "v=0..."}})
                assert res.status_code == 200, res.text
                assert res.json()["delivered"] == 1
                sig = await ws_b.receive_json()
                assert sig["event"] == "video_signal"
                assert sig["from"] == alice["id"]
                assert sig["data"] == {"type": "offer", "sdp": "v=0..."}

                # signaling over the socket itself: B answers back to A.
                # A has no live socket: the honest 0 in the ack
                await ws_b.send_json({"event": "video_signal",
                                      "to": alice["id"],
                                      "data": {"type": "answer", "sdp": "v=0..."}})
                ack = await ws_b.receive_json()
                assert ack["event"] == "video_signal_ack"
                assert ack["delivered"] == 0

                # A connects; the same relay now lands
                ws_a = _WSClient(app,
                                 f"/api/v1/voice/sessions/{alice['session_id']}/media",
                                 token=user["token"])
                await ws_a.connect()
                hello_a = await ws_a.receive_json()
                assert hello_a["event"] == "connected"
                await ws_b.send_json({"event": "video_signal",
                                      "to": alice["id"],
                                      "data": {"candidate": "cand-1"}})
                ack = await ws_b.receive_json()
                assert ack["delivered"] == 1
                sig = await ws_a.receive_json()
                assert sig["event"] == "video_signal"
                assert sig["data"] == {"candidate": "cand-1"}

                # a relay to a GHOST participant is refused on the wire
                await ws_b.send_json({"event": "video_signal", "to": "ghost",
                                      "data": {}})
                skip = await ws_b.receive_json()
                assert skip["event"] == "skipped"
                assert skip["reason"] == "video_signal_refused"

                # the HTTP relay to a non-participant refuses loudly
                res = await client.post(
                    f"/voice/meetings/{meeting['id']}/video/signal", headers=h,
                    json={"from_participant_id": alice["id"],
                          "to_participant_id": "ghost", "data": {}})
                assert res.status_code == 400

                # a NON-leg session's socket gets the honest refusal too
                loner = await _mk_live_session(client, h, frm="+15550006000")
                ws_x = _WSClient(app,
                                 f"/api/v1/voice/sessions/{loner['id']}/media",
                                 token=user["token"])
                await ws_x.connect()
                await ws_x.receive_json()  # connected
                await ws_x.send_json({"event": "video_signal", "to": alice["id"],
                                      "data": {}})
                skip = await ws_x.receive_json()
                assert skip["event"] == "skipped"
                assert "not a joined leg" in skip["detail"]
                await ws_x.close()

                # Alice stops the screen share: unpublish, video.stopped,
                # Bob's socket told
                res = await client.post(
                    f"/voice/meetings/{meeting['id']}/video/unpublish", headers=h,
                    json={"participant_id": alice["id"], "track_id": "screen-1"})
                assert res.status_code == 200, res.text
                assert res.json()["push"]["delivered"] == 1
                frame = await ws_b.receive_json()
                assert frame["event"] == "video_track"
                assert frame["action"] == "unpublished"
                res = await client.get(f"/voice/sessions/{alice['session_id']}",
                                       headers=h)
                kinds = [e["kind"] for e in res.json()["events"] or []]
                assert "video.stopped" in kinds
                res = await client.get(f"/voice/meetings/{meeting['id']}/video",
                                       headers=h)
                assert res.json()["video"]["counts"]["live_tracks"] == 1

                # a track that is already gone refuses honestly
                res = await client.post(
                    f"/voice/meetings/{meeting['id']}/video/unpublish", headers=h,
                    json={"participant_id": alice["id"], "track_id": "screen-1"})
                assert res.status_code == 400 and "not live" in res.json()["detail"]

                # the meeting detail rides the derived video block
                res = await client.get(f"/voice/meetings/{meeting['id']}", headers=h)
                assert res.json()["video"]["counts"]["live_tracks"] == 1
                assert res.json()["video"]["note"]

                await ws_a.close()
            finally:
                await ws_b.close()

            # an ended room takes no tracks and no signaling
            res = await client.post(f"/voice/meetings/{meeting['id']}/end", headers=h)
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/meetings/{meeting['id']}/video/publish",
                                    headers=h, json={"participant_id": alice["id"],
                                                     "track_id": "cam-2",
                                                     "kind": "camera"})
            assert res.status_code == 400 and "ended" in res.json()["detail"]
            res = await client.post(f"/voice/meetings/{meeting['id']}/video/signal",
                                    headers=h,
                                    json={"from_participant_id": alice["id"],
                                          "to_participant_id": bob["id"],
                                          "data": {}})
            assert res.status_code == 400

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5) the second voice solution: support-line-system, pre-wired
# ---------------------------------------------------------------------------

def test_v78_support_line_solution():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "sl")
            h = _auth(user["token"])

            # the shelf carries the new solution, flagged
            res = await client.get("/solutions", headers=h)
            assert res.status_code == 200, res.text
            shelf = {s["slug"]: s for s in res.json()["solutions"]}
            assert "support-line-system" in shelf
            assert shelf["support-line-system"]["support_line_ready"] is True
            assert shelf["support-line-system"]["category"] == "Voice"

            # the install refuses without the agent (the room binds it)
            res = await client.post("/solutions/support-line-system/install",
                                    headers=h, json={"as_support_line": True})
            assert res.status_code == 400
            assert "as_voice_agent" in res.json()["detail"]
            # ...and a solution with no support_line block refuses loudly
            res = await client.post("/solutions/voice-agent-system/install",
                                    headers=h,
                                    json={"as_voice_agent": True,
                                          "as_support_line": True})
            assert res.status_code == 400
            assert "support_line" in res.json()["detail"]

            # THE ONE-CLICK INSTALL: agent + room + queue pre-wired
            res = await client.post("/solutions/support-line-system/install",
                                    headers=h,
                                    json={"as_voice_agent": True,
                                          "as_support_line": True,
                                          "note": "the support line"})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["voice_agent"] and out["support_line"]
            sl = out["support_line"]
            agent_id = out["voice_agent"]["id"]
            assert sl["queue"]["config"]["announce"]["enabled"] is True
            assert sl["queue"]["config"]["sms"]["enabled"] is True
            assert sl["queue"]["config"]["callback"]["enabled"] is True
            # the knowledge landed too (dataset may be suffixed on collision)
            assert out["voice_agent"]["knowledge"]["dataset_id"]

            # the wiring, read back: the room is the queue's destination and
            # the agent's room; the queue's agent is the installed agent
            res = await client.get(f"/voice/queues/{sl['queue']['id']}", headers=h)
            assert res.status_code == 200, res.text
            qd = res.json()
            assert qd["meeting_id"] == sl["meeting"]["id"]
            assert qd["meeting_name"] == "Support line room"
            assert qd["agent_id"] == agent_id
            res = await client.get(f"/voice/meetings/{sl['meeting']['id']}",
                                   headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["agent_id"] == agent_id

            # the line WORKS: a caller waits, the callback trades the hold
            # (the dialer refuses honestly: no endpoint bound yet)
            s1 = await _mk_live_session(client, h, frm="+15550007000")
            res = await client.post(f"/voice/queues/{qd['id']}/entries",
                                    headers=h, json={"session_id": s1["id"]})
            assert res.status_code == 201, res.text
            entry_id = res.json()["entry_id"]
            res = await client.post(f"/voice/queues/{qd['id']}/callbacks",
                                    headers=h, json={"entry_id": entry_id})
            assert res.status_code == 200, res.text
            res = await client.get(f"/voice/sessions/{s1['id']}", headers=h)
            assert res.json()["end_reason"] == "callback"
            res = await client.post(f"/voice/queues/{qd['id']}/callbacks/dial",
                                    headers=h, json={})
            assert res.status_code == 200, res.text
            assert "nothing was dialed" in res.json()["start_note"]

            # the analytics are live from minute one
            res = await client.get(f"/voice/queues/{qd['id']}/analytics", headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["callbacks"]["requested"] == 1
            res = await client.get(f"/voice/meetings/{sl['meeting']['id']}/analytics",
                                   headers=h)
            assert res.status_code == 200, res.text

            # reinstalling is safe (names finalize, the wiring stays own-scoped)
            res = await client.post("/solutions/support-line-system/install",
                                    headers=h,
                                    json={"as_voice_agent": True,
                                          "as_support_line": True})
            assert res.status_code == 200, res.text
            sl2 = res.json()["support_line"]
            assert sl2["queue"]["id"] != sl["queue"]["id"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 6) the version pin
# ---------------------------------------------------------------------------

def test_v78_version_pin():
    from app.config import settings

    # relaxed to >= when the next batch pinned the new version
    assert tuple(int(p) for p in settings.version.split(".")) >= (1, 78, 0)
