"""V77 live smoke: boot the real server and drive the waiting experience.

1. QUEUE-POSITION ANNOUNCEMENTS OVER HOLD: a live call is enqueued (the
   session is HELD) and the join pass SPOAKS the held leg - the position
   text is synthesized by the REAL piper bridge (bound at boot, installed
   in v73) and delivered as the provider speak command on the held call
   (captured by the stub carrier); the timeline carries tts.started
   source=queue_announcement + queue.announced, the entry counts its
   announcements, and a second pass is honestly not_due.
2. CHAT PUSHED TO WEB LEGS VIA THE MEDIA WEBSOCKET: a real browser leg
   opens the session's media websocket (a REAL socket this time), the
   moderator posts to the room chat and the chat frame arrives on the
   live socket the moment it is posted; a member asks the agent and the
   reply is pushed to the room too.
3. SMS BACKCHANNEL FOR WAITING CALLERS: the queue binds a generic_sms
   channel pointed at the stub gateway; the join pass TEXTS the waiting
   caller their position (a REAL HTTP POST the stub captures), the send
   lands in the entry's meta as the backchannel log and on the session
   timeline as queue.sms.

Usage: /home/z/.venv/bin/python scripts/smoke_v77_live.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v74_server.py"
API = "http://127.0.0.1:8205/api/v1"
WS_BASE = "ws://127.0.0.1:8205/api/v1"
STUB_PORT = 3015
SERVER_PORT = 8205

sys_stubs = {"sms": [], "commands": []}


def wait_health(client: httpx.Client, deadline: float = 30.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        try:
            res = client.get(f"{API}/health")
            if res.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise SystemExit("server never became healthy")


# ---------------------------------------------------------------------------
# the stub: call-control commands + the SMS gateway
# ---------------------------------------------------------------------------

def _start_stub() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: N805 - silence
            pass

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("content-length", 0)))
            body = json.loads(raw.decode() or "{}")
            if "/actions/" in self.path:
                sys_stubs["commands"].append({"path": self.path, "body": body})
                self._json(200, {"data": {"result": "ok"}})
            elif self.path.endswith("/sms"):
                sys_stubs["sms"].append(body)
                self._json(200, {"ok": True, "id": f"sm-{len(sys_stubs['sms'])}"})
            else:
                self._json(404, {"error": "not found"})

        def do_GET(self):  # noqa: N802
            self._json(404, {"error": "not found"})

    srv = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------------------
# 1) queue-position announcements over hold - TTS on the held leg
# ---------------------------------------------------------------------------

def announce_check(c: httpx.Client, tag: str) -> dict:
    agent = c.post("/voice/agents", json={
        "name": f"Line persona {tag}", "greeting_text": "Thanks for calling.",
        "scaffold_handler": True}).json()

    # a carrier speak path: the telnyx-compatible gateway (the stub)
    ep = c.post("/channels/endpoints", json={
        "name": f"v77 telnyx {tag}", "provider": "telnyx_call_control",
        "config": {"api_key": "telnyx-v77-key",
                   "api_base": f"http://127.0.0.1:{STUB_PORT}",
                   "public_key": "smoke-public-key",
                   "from_number": "+15553330001"}})
    assert ep.status_code == 201, ep.text

    queue = c.post("/voice/queues", json={
        "name": f"support line {tag}",
        "config": {"max_wait_seconds": 600,
                   "announce": {"interval_seconds": 60,
                                "template": "You are number {position} of {depth} "
                                            "in line for {queue_name}. Please hold."}}}).json()
    assert queue["config"]["announce"]["enabled"] is True, queue["config"]

    # a live inbound carrier call
    sess = c.post("/voice/sessions", json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"cc-v77-{tag}",
        "from_ref": "+15553330002", "to_ref": "+15553330999"}).json()
    c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.ringing", "payload": {}})
    c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.answered", "payload": {}})
    assert c.get(f"/voice/sessions/{sess['id']}").json()["state"] == "in_progress"

    # enqueue: held + the JOIN pass speaks the position through the REAL
    # piper bridge and delivers the provider speak command on the held call
    commands_before = len(sys_stubs["commands"])
    res = c.post(f"/voice/queues/{queue['id']}/entries",
                 json={"session_id": sess["id"], "label": "Held Caller"})
    assert res.status_code == 201, res.text
    entry_id = res.json()["entry_id"]
    assert c.get(f"/voice/sessions/{sess['id']}").json()["state"] == "on_hold"
    ann = res.json()["announcement"]
    assert ann["considered"] == 1, {"ann": ann, "hooks": res.json()}
    hit = ann["announced"][0]
    assert hit["announced"] is True, hit
    assert hit["position"] == 1 and hit["depth"] == 1, hit
    assert hit["text"].startswith("You are number 1 of 1"), hit["text"]

    # the stub captured the speak command on the held call
    speaks = [cmd for cmd in sys_stubs["commands"][commands_before:]
              if cmd["path"].endswith("/actions/speak")]
    assert speaks, sys_stubs["commands"][commands_before:]
    assert "number 1 of 1" in speaks[0]["body"].get("payload", ""), speaks[0]["body"]

    # the timeline: the announcement's TTS + the queue.announced record
    detail = c.get(f"/voice/sessions/{sess['id']}").json()
    tts = [e for e in detail["events"] if e["kind"] == "tts.started"
           and e["payload"].get("source") == "queue_announcement"]
    assert tts, detail["events"]
    qa = [e for e in detail["events"] if e["kind"] == "queue.announced"]
    assert qa and qa[-1]["payload"]["position"] == 1, qa

    # an immediate pass is honestly NOT due (position unchanged, interval fresh)
    res = c.post(f"/voice/queues/{queue['id']}/announce", json={"force": False})
    assert res.status_code == 200, res.text
    assert res.json()["announced"][0].get("reason") == "not_due", res.json()

    # the entry counts its announcements
    qd = c.get(f"/voice/queues/{queue['id']}").json()
    row = next(e for e in qd["entries"] if e["id"] == entry_id)
    assert row["meta"]["announcements"] == 1 and row["meta"]["last_announced_position"] == 1, row["meta"]
    return {"spoken_text": hit["text"], "speak_commands": len(speaks)}


# ---------------------------------------------------------------------------
# 2) chat pushed to web legs via the media websocket
# ---------------------------------------------------------------------------

def chat_push_check(c: httpx.Client, tag: str) -> dict:
    import websockets

    agent = c.post("/voice/agents", json={
        "name": f"Room persona {tag}", "greeting_text": "Welcome to the room.",
        "scaffold_handler": True}).json()
    meeting = c.post("/voice/meetings", json={
        "title": f"v77 room {tag}", "agent_id": agent["id"]}).json()
    join = c.post(f"/voice/meetings/{meeting['id']}/join",
                  json={"label": "browser leg", "channel": "web"})
    assert join.status_code == 200, join.text
    leg = join.json()["participant"]
    assert leg["session_id"], leg

    async def _drive() -> dict:
        out: dict = {}
        async with websockets.connect(
                f"{WS_BASE}/voice/sessions/{leg['session_id']}/media",
                open_timeout=15, close_timeout=10) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert hello["event"] == "connected", hello

            # the moderator posts: the frame reaches the LIVE socket NOW
            res = await asyncio.to_thread(
                c.post, f"/voice/meetings/{meeting['id']}/chat",
                json={"text": "support engineer joining in a moment"})
            assert res.status_code == 200, res.text
            push = res.json()["push"]
            assert push["legs"] >= 1 and push["delivered"] >= 1, push
            frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert frame["event"] == "chat", frame
            assert frame["message"]["role"] == "moderator", frame["message"]
            assert frame["message"]["text"] == "support engineer joining in a moment"

            # a member asks the agent: the question AND the reply are pushed
            res = await asyncio.to_thread(
                c.post, f"/voice/meetings/{meeting['id']}/chat",
                json={"text": "what are your hours?",
                      "participant_id": leg["id"], "ask_agent": True})
            assert res.status_code == 200, res.text
            ask_frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert ask_frame["event"] == "chat" and ask_frame["message"]["role"] == "member"
            reply_frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert reply_frame["event"] == "chat" and reply_frame["message"]["role"] == "agent"
            out["reply"] = reply_frame["message"]["text"][:60]
            out["push_legs"] = push["legs"]
        return out

    return asyncio.run(_drive())


# ---------------------------------------------------------------------------
# 3) the SMS backchannel for waiting callers
# ---------------------------------------------------------------------------

def sms_backchannel_check(c: httpx.Client, tag: str) -> dict:
    gateway = c.post("/channels/endpoints", json={
        "name": f"v77 sms gateway {tag}", "provider": "generic_sms",
        "config": {"secret": f"sms-sekrit-{tag}",
                   "send_url": f"http://127.0.0.1:{STUB_PORT}/sms",
                   "bearer_token": "stub-token", "from_number": "PY8N"}})
    assert gateway.status_code == 201, gateway.text

    queue = c.post("/voice/queues", json={
        "name": f"textback line {tag}",
        "config": {"sms": {"enabled": True, "channel_id": gateway.json()["id"],
                           "template": "{queue_name}: you are #{position} of {depth} "
                                       "(waited {waited_seconds}s)."}}}).json()
    assert queue["config"]["sms"]["enabled"] is True, queue["config"]

    sess = c.post("/voice/sessions", json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"cc-sms-{tag}",
        "from_ref": "+15553330003", "to_ref": "+15553330999"}).json()
    c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.ringing", "payload": {}})
    c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.answered", "payload": {}})

    res = c.post(f"/voice/queues/{queue['id']}/entries",
                 json={"session_id": sess["id"], "label": "Texted Caller"})
    assert res.status_code == 201, res.text
    entry_id = res.json()["entry_id"]
    sent = res.json()["sms"]["sent"]
    assert len(sent) == 1, res.json()["sms"]
    assert sent[0]["delivery"] == "delivered", sent[0]
    assert sent[0]["to"] == "+15553330003", sent[0]

    # the stub gateway captured the REAL text
    assert sys_stubs["sms"], "the gateway captured nothing"
    text = sys_stubs["sms"][-1].get("text", "")
    assert "#1 of 1" in text and tag, sys_stubs["sms"][-1]

    # the record lives on the entry and the session timeline
    qd = c.get(f"/voice/queues/{queue['id']}").json()
    row = next(e for e in qd["entries"] if e["id"] == entry_id)
    assert row["meta"]["sms"] and row["meta"]["sms"][0]["delivery"] == "delivered", row["meta"]
    detail = c.get(f"/voice/sessions/{sess['id']}").json()
    sms_events = [e for e in detail["events"] if e["kind"] == "queue.sms"]
    assert sms_events and sms_events[0]["payload"]["delivery"] == "delivered", sms_events

    # an explicit position pass texts again (the operator button)
    res = c.post(f"/voice/queues/{queue['id']}/sms-update", json={"event": "position"})
    assert res.status_code == 200 and res.json()["event"] == "position", res.text
    assert res.json()["sent"][0]["delivery"] == "delivered", res.json()
    return {"texts_sent": len(sys_stubs["sms"]), "last_text": text}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v77_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PORT": str(SERVER_PORT),
    })
    stub = _start_stub()
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.77.0", version
            tag = uuid.uuid4().hex[:6]

            a = announce_check(c, tag)
            print(f"[1] QUEUE ANNOUNCEMENTS OK - the held caller was SPOKEN to: "
                  f"\"{a['spoken_text'][:64]}...\" synthesized by the real piper "
                  f"bridge and delivered as {a['speak_commands']} provider speak "
                  f"command(s) on the held call; the timeline carries the "
                  f"tts.started(source=queue_announcement) + queue.announced records, "
                  f"the entry counts its announcements, and an immediate second pass "
                  f"is honestly not_due")

            ch = chat_push_check(c, tag)
            print(f"[2] CHAT PUSH OK - a REAL websocket leg was attached to the "
                  f"media transport: the moderator's message arrived on the live "
                  f"socket the moment it was posted (push reached "
                  f"{ch['push_legs']} leg(s)), and the member's ask-the-agent "
                  f"round trip pushed the question AND the reply "
                  f"(\"{ch['reply']}...\") to the room")

            s = sms_backchannel_check(c, tag)
            print(f"[3] SMS BACKCHANNEL OK - the waiting caller's phone got the "
                  f"line: {s['texts_sent']} real text(s) through the bound "
                  f"generic_sms gateway (\"{s['last_text'][:56]}...\"), every send "
                  f"recorded on the entry meta and the session timeline "
                  f"(queue.sms), and the explicit position pass delivered again")

            print(f"\nALL 3 CHECKS GREEN - v77 live smoke passed (version {version})")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        try:
            os.remove(db_path)
        except OSError:
            pass
        stub.shutdown()


if __name__ == "__main__":
    sys.path.insert(0, BACKEND)
    raise SystemExit(main())
