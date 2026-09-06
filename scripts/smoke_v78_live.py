"""V78 live smoke: boot the real server and drive callbacks, video, the
measured line/room, and the support-line system.

1. CALLBACKS INSTEAD OF HOLD (queue -> campaign composition): two live
   carrier calls wait in the queue; each trades the hold for a callback
   (the hold ends honestly with reason=callback, a target is booked on
   the queue's COMPOSED campaign); the dial pass places REAL dials
   through the stub carrier; a SIGNED call.answered webhook opens the
   answered session and the attach hook walks the callback into the
   queue's destination meeting ON THE CALLBACK CALL; the analytics
   endpoint measures the line.
2. FIRST-CLASS VIDEO: two web legs in a room; a REAL media websocket on
   leg B; publishing a camera + a screen track pushes video_track frames
   to the live socket; the HTTP signaling relay delivers a WebRTC offer
   to the live socket; a relay from the socket itself gets the honest
   delivered count; unpublishing pushes the news.
3. SUPPORT LINE SYSTEM: the marketplace solution installs AS a support
   line (agent + room + queue pre-wired with announce + SMS + callback
   blocks); a caller trades the hold; the dial pass refuses HONESTLY
   (no endpoint bound yet - the installer's credential); the analytics
   are live from minute one.

Usage: /home/z/.venv/bin/python scripts/smoke_v78_live.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
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
API = "http://127.0.0.1:8206/api/v1"
WS_BASE = "ws://127.0.0.1:8206/api/v1"
STUB_PORT = 3016
SERVER_PORT = 8206

sys_stubs = {"dials": [], "commands": []}


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
            if self.path.endswith("/v2/calls"):
                sys_stubs["dials"].append(body)
                self._json(200, {"data": {"call_control_id": f"CC-v78-{len(sys_stubs['dials'])}",
                                          "call_session_id": f"CS-{len(sys_stubs['dials'])}"}}) 
            elif "/actions/" in self.path:
                sys_stubs["commands"].append({"path": self.path, "body": body})
                self._json(200, {"data": {"result": "ok"}})
            else:
                self._json(404, {"error": "not found"})

        def do_GET(self):  # noqa: N802
            self._json(404, {"error": "not found"})

    srv = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# RFC 9421 signing for the carrier's webhooks back to py8n
def _ed25519_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub_pem


def _rfc9421_headers(priv, raw: bytes, target: str) -> dict:
    components = ("@method", "@target", "content-digest")
    covered = " ".join(f'"{c}"' for c in components)
    sig_input = f'sig1=({covered});created=1618884473;keyid="k1"'
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode()
    lines = [f'"@method": POST', f'"@target": {target}',
             f'"content-digest": sha-256=:{digest}:']
    lines.append(f'"@signature-params": ({covered});created=1618884473;keyid="k1"')
    sig = base64.b64encode(priv.sign("\n".join(lines).encode("utf-8"))).decode()
    return {"signature-input": sig_input, "signature": f"sig1=:{sig}:",
            "content-digest": f"sha-256=:{digest}:"}


def _client_state(campaign_id: str, target_id: str) -> str:
    raw = json.dumps({"cmp": campaign_id, "tgt": target_id}).encode()
    return base64.b64encode(raw).decode("ascii")


def _mk_live_call(c: httpx.Client, call_ref: str, frm: str) -> dict:
    sess = c.post("/voice/sessions", json={
        "direction": "inbound", "provider": "telnyx", "call_ref": call_ref,
        "from_ref": frm, "to_ref": "+15554440999"}).json()
    c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.ringing", "payload": {}})
    c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.answered", "payload": {}})
    assert c.get(f"/voice/sessions/{sess['id']}").json()["state"] == "in_progress"
    return sess


# ---------------------------------------------------------------------------
# 1) callbacks instead of hold - queue -> campaign composition, live
# ---------------------------------------------------------------------------

def callbacks_check(c: httpx.Client, tag: str) -> dict:
    priv, pub_pem = _ed25519_keypair()

    agent = c.post("/voice/agents", json={
        "name": f"Callback persona {tag}", "greeting_text": "Thanks for calling.",
        "scaffold_handler": True}).json()
    meeting = c.post("/voice/meetings", json={
        "title": f"v78 callback room {tag}", "agent_id": agent["id"]}).json()
    ep = c.post("/channels/endpoints", json={
        "name": f"v78 cb telnyx {tag}", "provider": "telnyx_call_control",
        "config": {"api_key": "telnyx-v78-key",
                   "api_base": f"http://127.0.0.1:{STUB_PORT}/v2",
                   "public_key": pub_pem,
                   "connection_id": "conn-v78",
                   "webhook_url": f"http://127.0.0.1:{SERVER_PORT}/api/v1/channels/telnyx/v78/webhook",
                   "from_number": "+15554440001"}})
    assert ep.status_code == 201, ep.text
    ep_id = ep.json()["id"]
    hook_path = f"/api/v1/channels/telnyx/{ep_id}/webhook"
    queue = c.post("/voice/queues", json={
        "name": f"callback line {tag}", "agent_id": agent["id"],
        "meeting_id": meeting["id"],
        "config": {"callback": {"enabled": True, "endpoint_id": ep_id}}}).json()
    assert queue["config"]["callback"]["enabled"] is True, queue["config"]

    # two live callers wait, then BOTH trade the hold for a callback
    sa = _mk_live_call(c, f"cc-cb-{tag}-a", "+15554440002")
    sb = _mk_live_call(c, f"cc-cb-{tag}-b", "+15554440003")
    entries = []
    for s in (sa, sb):
        res = c.post(f"/voice/queues/{queue['id']}/entries",
                     json={"session_id": s["id"], "label": f"caller-{s['id'][:6]}"})
        assert res.status_code == 201, res.text
        entries.append(res.json()["entry_id"])
    for entry_id in entries:
        res = c.post(f"/voice/queues/{queue['id']}/callbacks",
                     json={"entry_id": entry_id})
        assert res.status_code == 200, res.text
    for s in (sa, sb):
        detail = c.get(f"/voice/sessions/{s['id']}").json()
        assert detail["state"] == "ended" and detail["end_reason"] == "callback", detail["state"]
    qd = c.get(f"/voice/queues/{queue['id']}").json()
    assert qd["depth"]["callback"] == 2 and qd["callbacks"]["requested"] == 2, qd["depth"]
    campaign_id = qd["callbacks"]["campaign_id"]
    assert qd["callbacks"]["counts"]["pending"] == 2, qd["callbacks"]

    # the dial pass places REAL dials through the stub carrier
    dials_before = len(sys_stubs["dials"])
    res = c.post(f"/voice/queues/{queue['id']}/callbacks/dial", json={})
    assert res.status_code == 200, res.text
    assert res.json()["start_note"] == "2 dial(s) placed", res.json()["start_note"]
    dials = sys_stubs["dials"][dials_before:]
    assert len(dials) == 2, sys_stubs["dials"]
    assert "to" in dials[0], dials[0]

    # the SIGNED answer webhook for the FIRST dial: the answered session
    # walks into the destination meeting on the callback call itself
    tgt = c.get(f"/voice/campaigns/{campaign_id}").json()["targets"][0]
    payload = {"data": {"event_type": "call.answered",
                        "payload": {"call_control_id": tgt["call_control_id"],
                                    "client_state": _client_state(campaign_id, tgt["id"])}}}
    raw = json.dumps(payload).encode()
    res = httpx.post(f"http://127.0.0.1:{SERVER_PORT}{hook_path}", content=raw,
                     headers=_rfc9421_headers(priv, raw, hook_path), timeout=30)
    assert res.status_code == 200, res.text
    handled = [h for h in res.json().get("handled", []) if h.get("session_id")]
    assert handled, res.text
    cb_session = handled[0]["session_id"]

    md = c.get(f"/voice/meetings/{meeting['id']}").json()
    joined = [p for p in md["participants"] if p["session_id"] == cb_session]
    assert joined and joined[0]["state"] == "joined", md["participants"]

    # the line, measured (live): the callback picture + the abandonment
    an = c.get(f"/voice/queues/{queue['id']}/analytics").json()
    assert an["callbacks"]["requested"] == 2, an["callbacks"]
    assert an["callbacks"]["counts"].get("dialing", 0) >= 1, an["callbacks"]
    assert an["outcomes"]["callback"] == 2, an["outcomes"]
    return {"dials": len(dials), "callback_address": dials[0].get("to"),
            "meeting_legs": md["counts"]["participants"]}


# ---------------------------------------------------------------------------
# 2) first-class video - tracks + the signaling relay on live sockets
# ---------------------------------------------------------------------------

def video_check(c: httpx.Client, tag: str) -> dict:
    import websockets

    agent = c.post("/voice/agents", json={
        "name": f"Video persona {tag}", "greeting_text": "Welcome on camera.",
        "scaffold_handler": True}).json()
    meeting = c.post("/voice/meetings", json={
        "title": f"v78 video room {tag}", "agent_id": agent["id"]}).json()
    legs = []
    for label in ("Alice", "Bob"):
        leg = c.post(f"/voice/meetings/{meeting['id']}/join",
                     json={"label": label, "channel": "web"}).json()["participant"]
        legs.append(leg)

    async def _drive() -> dict:
        out: dict = {}
        async with websockets.connect(
                f"{WS_BASE}/voice/sessions/{legs[1]['session_id']}/media",
                open_timeout=15, close_timeout=10) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert hello["event"] == "connected", hello

            # Alice's camera: the registry + a live push to Bob's socket
            res = await asyncio.to_thread(
                c.post, f"/voice/meetings/{meeting['id']}/video/publish",
                json={"participant_id": legs[0]["id"], "track_id": f"cam-{tag}",
                      "kind": "camera"})
            assert res.status_code == 200, res.text
            assert res.json()["push"]["delivered"] == 1, res.json()["push"]
            frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert frame["event"] == "video_track" and frame["action"] == "published", frame
            assert frame["track_id"] == f"cam-{tag}"

            # the screen share: pushed live too, the state names the holder
            res = await asyncio.to_thread(
                c.post, f"/voice/meetings/{meeting['id']}/video/publish",
                json={"participant_id": legs[0]["id"], "track_id": f"scr-{tag}",
                      "kind": "screen", "label": "the deck"})
            assert res.status_code == 200, res.text
            frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert frame["event"] == "video_track" and frame["kind"] == "screen", frame
            state = c.get(f"/voice/meetings/{meeting['id']}/video").json()["video"]
            assert state["screen_holders"] == ["Alice"], state
            out["live_tracks"] = state["counts"]["live_tracks"]

            # the signaling relay over HTTP: Alice offers Bob - delivered 1
            res = await asyncio.to_thread(
                c.post, f"/voice/meetings/{meeting['id']}/video/signal",
                json={"from_participant_id": legs[0]["id"],
                      "to_participant_id": legs[1]["id"],
                      "data": {"type": "offer", "sdp": f"v=0 offer-{tag}"}})
            assert res.status_code == 200 and res.json()["delivered"] == 1, res.text
            sig = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert sig["event"] == "video_signal" and sig["data"]["type"] == "offer", sig

            # the socket itself signals BACK (Bob answers) - Bob's own ack
            # reports the honest count (Alice has no live socket here: 0)
            await ws.send(json.dumps({"event": "video_signal",
                                      "to": legs[0]["id"],
                                      "data": {"type": "answer", "sdp": f"v=0 answer-{tag}"}}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert ack["event"] == "video_signal_ack" and ack["delivered"] == 0, ack

            # a ghost participant is refused ON the wire
            await ws.send(json.dumps({"event": "video_signal", "to": "ghost", "data": {}}))
            skip = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert skip["event"] == "skipped" and skip["reason"] == "video_signal_refused", skip

            # the screen share ends: the unpublish is pushed live
            res = await asyncio.to_thread(
                c.post, f"/voice/meetings/{meeting['id']}/video/unpublish",
                json={"participant_id": legs[0]["id"], "track_id": f"scr-{tag}"})
            assert res.status_code == 200 and res.json()["push"]["delivered"] == 1, res.text
            frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert frame["event"] == "video_track" and frame["action"] == "unpublished", frame
        return out

    out = asyncio.run(_drive())
    detail = c.get(f"/voice/sessions/{legs[0]['session_id']}").json()
    kinds = [e["kind"] for e in detail["events"] or []]
    assert "video.started" in kinds and "video.stopped" in kinds, kinds
    return {**out, "timeline": sorted(set(k for k in kinds if k.startswith("video")))}


# ---------------------------------------------------------------------------
# 3) the support-line-system solution - one click, pre-wired
# ---------------------------------------------------------------------------

def support_line_check(c: httpx.Client, tag: str) -> dict:
    shelf = {s["slug"]: s for s in c.get("/solutions").json()["solutions"]}
    assert shelf["support-line-system"]["support_line_ready"] is True, list(shelf)
    install = c.post("/solutions/support-line-system/install",
                     json={"as_voice_agent": True, "as_support_line": True,
                           "note": f"smoke {tag}"})
    assert install.status_code == 200, install.text
    sl = install.json()["support_line"]
    assert install.json()["voice_agent"], install.json()
    cfg = sl["queue"]["config"]
    assert cfg["announce"]["enabled"] and cfg["sms"]["enabled"] \
        and cfg["callback"]["enabled"], cfg

    qd = c.get(f"/voice/queues/{sl['queue']['id']}").json()
    assert qd["meeting_id"] == sl["meeting"]["id"], qd["meeting_id"]
    md = c.get(f"/voice/meetings/{sl['meeting']['id']}").json()
    assert md["agent_id"] == install.json()["voice_agent"]["id"], md["agent_id"]

    # the line works from minute one: a caller waits and trades the hold
    sess = _mk_live_call(c, f"cc-sl-{tag}", "+15554440004")
    res = c.post(f"/voice/queues/{qd['id']}/entries",
                 json={"session_id": sess["id"], "label": "smoke caller"})
    assert res.status_code == 201, res.text
    entry_id = res.json()["entry_id"]
    res = c.post(f"/voice/queues/{qd['id']}/callbacks", json={"entry_id": entry_id})
    assert res.status_code == 200, res.text
    detail = c.get(f"/voice/sessions/{sess['id']}").json()
    assert detail["end_reason"] == "callback", detail["end_reason"]

    # the dial pass refuses HONESTLY (no dialing endpoint bound yet - the
    # installer's credential; the target is marked skipped with the reason)
    res = c.post(f"/voice/queues/{qd['id']}/callbacks/dial", json={})
    assert res.status_code == 200, res.text
    assert "nothing was dialed" in res.json()["start_note"], res.json()["start_note"]

    # the analytics are live: one callback, the room measured
    an = c.get(f"/voice/queues/{qd['id']}/analytics").json()
    assert an["callbacks"]["requested"] == 1, an["callbacks"]
    man = c.get(f"/voice/meetings/{sl['meeting']['id']}/analytics").json()
    assert man["meeting_id"] == sl["meeting"]["id"], man["meeting_id"]
    return {"queue": qd["name"], "room": md["title"],
            "callback_counts": an["callbacks"]["counts"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v78_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.78.0", version
            tag = uuid.uuid4().hex[:6]

            cb = callbacks_check(c, tag)
            print(f"[1] CALLBACKS OK - two callers traded the hold for a callback "
                  f"(reason=callback), the composed campaign placed {cb['dials']} REAL "
                  f"dial(s) through the stub carrier (to {cb['callback_address']}), and a "
                  f"SIGNED call.answered webhook walked the callback into the destination "
                  f"meeting ({cb['meeting_legs']} leg(s) in the room); the analytics "
                  f"endpoint measured the line live")

            v = video_check(c, tag)
            print(f"[2] VIDEO OK - first-class video on live sockets: {v['live_tracks']} "
                  f"live track(s) in the registry, camera + screen publishes and the "
                  f"unpublish pushed to the REAL media websocket, the WebRTC offer "
                  f"delivered through the signaling relay (honest counts both ways), "
                  f"timeline carries {v['timeline']}")

            sl = support_line_check(c, tag)
            print(f"[3] SUPPORT LINE SYSTEM OK - the marketplace solution installed as a "
                  f"support line: queue \"{sl['queue']}\" wired to room \"{sl['room']}\" "
                  f"with spoken positions + SMS + callbacks pre-configured; the caller "
                  f"traded the hold and the un-bound dialer refused honestly; "
                  f"callback counts {sl['callback_counts']}")

            print(f"\nALL 3 CHECKS GREEN - v78 live smoke passed (version {version})")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        stub.shutdown()
        for f in (db_path, db_path + "-wal", db_path + "-shm"):
            try:
                os.unlink(f)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
