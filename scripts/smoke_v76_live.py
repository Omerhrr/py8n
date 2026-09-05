"""V76 live smoke: boot the real server and drive the batch end to end.

1. CHANNEL QUEUE: a waiting room with a destination meeting - a live call
   is enqueued (the session is HELD, on_hold), FIFO positions derive, and
   seating the head releases the call and ATTACHES the same live call to
   the room as a leg (the room's agent binds).
2. VOICEMAIL DROP: a campaign with amd {mode: greeting_end, on_machine:
   voicemail_drop} dials a REAL number against the stub carrier (the dial
   carries machine_detection: "greeting_end"); the carrier's SIGNED
   greeting_ended webhook walks the session in_progress -> voicemail ->
   ended (voicemail_drop) while the stub captures the speak command with
   the configured message followed by the hangup command; the target
   books meta.voicemail_drop; the honest simulate door walks the same
   drop without a carrier.
3. ROOM CHAT + MODERATOR QUEUE: the moderator and a member post to the
   room's chat, the member asks the agent (the reply lands in the chat
   AND on the member's leg conversation), two hands raise, calling next
   grants the floor through the directed-floor primitive.

Usage: /home/z/.venv/bin/python scripts/smoke_v76_live.py
"""

from __future__ import annotations

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
API = "http://127.0.0.1:8204/api/v1"
STUB_PORT = 3014
SERVER_PORT = 8204

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


# ---------------------------------------------------------------------------
# the carrier stub: dials + call-control commands
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
            if self.path.endswith("/v2/calls"):
                sys_stubs["dials"].append(body)
                self._json(200, {"data": {"call_control_id": f"CC-v76-{len(sys_stubs['dials'])}",
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


# ---------------------------------------------------------------------------
# 1) channel queues: hold, position, seat into the room on the SAME call
# ---------------------------------------------------------------------------

def channel_queue_check(c: httpx.Client, tag: str) -> dict:
    agent = c.post("/voice/agents", json={
        "name": f"Queue room persona {tag}", "greeting_text": "Thanks for holding.",
        "scaffold_handler": True}).json()
    meeting = c.post("/voice/meetings", json={
        "title": f"v76 queue room {tag}", "agent_id": agent["id"]}).json()
    queue = c.post("/voice/queues", json={
        "name": f"support line {tag}", "meeting_id": meeting["id"],
        "config": {"max_size": 5, "max_wait_seconds": 600}}).json()
    assert queue["state"] == "open" and queue["depth"]["waiting"] == 0, queue

    # a live inbound call
    sess = c.post("/voice/sessions", json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"cc-queue-{tag}",
        "from_ref": "+15552220001", "to_ref": "+15552220999"}).json()
    c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.ringing", "payload": {}})
    c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.answered", "payload": {}})
    assert c.get(f"/voice/sessions/{sess['id']}").json()["state"] == "in_progress"

    # enqueue: the call is HELD and books its place
    res = c.post(f"/voice/queues/{queue['id']}/entries",
                 json={"session_id": sess["id"], "label": "Waiting Caller"})
    assert res.status_code == 201, res.text
    entry_id = res.json()["entry_id"]
    assert c.get(f"/voice/sessions/{sess['id']}").json()["state"] == "on_hold", \
        "the waiting call is held"
    body = c.get(f"/voice/queues/{queue['id']}").json()
    assert body["depth"]["waiting"] == 1, body["depth"]
    assert body["entries"][0]["position"] == 1, body["entries"]
    assert body["entries"][0]["waited_seconds"] is not None

    # seating the head: released AND attached to the destination meeting
    seat = c.post(f"/voice/queues/{queue['id']}/next", json={}).json()
    assert seat["seated"]["label"] == "Waiting Caller", seat
    assert seat["seated"]["released_state"] == "in_progress", seat
    assert seat["attached"]["meeting_id"] == meeting["id"], seat
    room = c.get(f"/voice/meetings/{meeting['id']}").json()
    legs = [p for p in room["participants"] if p["session_id"] == sess["id"]]
    assert legs and legs[0]["state"] == "joined", room["participants"]
    assert room["counts"]["joined"] == 1, room["counts"]
    assert c.get(f"/voice/sessions/{sess['id']}").json()["state"] == "in_progress"
    detail = c.get(f"/voice/queues/{queue['id']}").json()
    seated_row = next(e for e in detail["entries"] if e["id"] == entry_id)
    assert seated_row["status"] == "seated", seated_row

    # nobody left: an honest refusal
    empty = c.post(f"/voice/queues/{queue['id']}/next", json={})
    assert empty.status_code == 400 and "nobody" in empty.json()["detail"], empty.text
    return {"waiting_after": detail["depth"]["waiting"], "leg_joined": True}


# ---------------------------------------------------------------------------
# 2) the voicemail drop: greeting_end triggers the message + hangup
# ---------------------------------------------------------------------------

def voicemail_drop_check(c: httpx.Client, tag: str, priv, pub_pem: str) -> dict:
    agent = c.post("/voice/agents", json={
        "name": f"Drop persona {tag}", "greeting_text": "Hello?",
        "scaffold_handler": True}).json()
    ep = c.post("/channels/endpoints", json={
        "name": f"v76 telnyx {tag}", "provider": "telnyx_call_control",
        "config": {"api_key": "telnyx-v76-key",
                   "api_base": f"http://127.0.0.1:{STUB_PORT}/v2",
                   "public_key": pub_pem,
                   "connection_id": "conn-v76",
                   "webhook_url": f"http://127.0.0.1:{SERVER_PORT}/api/v1/channels/telnyx/v76/webhook",
                   "from_number": "+155522208"}})
    assert ep.status_code == 201, ep.text
    ep_id = ep.json()["id"]

    message = f"Hi, this is py8n calling about your renewal. Goodbye. ({tag})"
    camp = c.post("/voice/campaigns", json={
        "name": f"v76 renewal drops {tag}", "agent_id": agent["id"],
        "endpoint_id": ep_id,
        "targets": [{"address": "+15552220002", "name": "answering machine"}],
        "config": {"amd": {"mode": "greeting_end", "on_machine": "voicemail_drop",
                           "voicemail_message": message}}})
    assert camp.status_code == 201, camp.text
    camp = camp.json()
    assert camp["config"]["amd"]["voicemail_message"] == message, camp["config"]["amd"]

    start = c.post(f"/voice/campaigns/{camp['id']}/start", json={}).json()
    assert start["status"] == "running", start
    assert len(sys_stubs["dials"]) == 1, sys_stubs["dials"]
    assert sys_stubs["dials"][0].get("machine_detection") == "greeting_end", \
        "greeting_end detection rides the dial"
    cc = start["targets"][0]["call_control_id"]
    tgt_id = start["targets"][0]["id"]

    hook_path = f"/api/v1/channels/telnyx/{ep_id}/webhook"
    url = f"http://127.0.0.1:{SERVER_PORT}{hook_path}"

    def _post_carrier(payload: dict) -> dict:
        raw = json.dumps(payload).encode()
        res = httpx.post(url, content=raw,
                         headers=_rfc9421_headers(priv, raw, hook_path), timeout=30)
        assert res.status_code == 200, res.text
        return res.json()

    # the carrier walk: dial -> answered -> the greeting ENDS
    _post_carrier({"data": {"event_type": "call.initiated",
                            "payload": {"call_control_id": cc, "direction": "outgoing"}}})
    out = _post_carrier({"data": {"event_type": "call.answered",
                                  "payload": {"call_control_id": cc,
                                              "client_state": _client_state(camp["id"], tgt_id)}}})
    handled = out["handled"][-1]
    assert handled["state"] == "in_progress", handled
    session_id = handled["session_id"]
    sess_before = c.get(f"/voice/sessions/{session_id}").json()
    commands_before = len(sys_stubs["commands"])

    out = _post_carrier({"data": {"event_type": "call.machine.detection.ended",
                                  "payload": {"call_control_id": cc,
                                              "result": "greeting_ended"}}})
    handled = out["handled"][-1]
    assert "vm_drop_speak_built" in handled["actions"], handled
    assert "vm_drop_hangup_built" in handled["actions"], handled
    assert handled["state"] == "ended" and handled["end_reason"] == "voicemail_drop", handled

    # the stub captured the drop: a speak command with the message,
    # then the hangup - the wire order a real drop needs
    new_commands = sys_stubs["commands"][commands_before:]
    speaks = [cmd for cmd in new_commands if cmd["path"].endswith("/actions/speak")]
    hangups = [cmd for cmd in new_commands if cmd["path"].endswith("/actions/hangup")]
    assert speaks and speaks[0]["body"].get("payload") == message, new_commands
    assert hangups, "the drop hangs up after the message"

    # the session timeline carries the drop utterance; the target books it
    sess_after = c.get(f"/voice/sessions/{session_id}").json()
    drops = [e for e in sess_after["events"] if e["kind"] == "tts.started"
             and e["payload"].get("source") == "voicemail_drop"]
    assert drops, sess_after["events"]
    detail = c.get(f"/voice/campaigns/{camp['id']}").json()
    t1 = next(t for t in detail["targets"] if t["id"] == tgt_id)
    assert t1["status"] == "voicemail" and t1["amd"]["result"] == "greeting_ended", t1
    assert t1["voicemail_drop"]["message"] == message, t1
    assert detail["progress"]["counts"]["voicemail"] == 1, detail["progress"]

    # the honest simulate door walks the greeting_end drop without a carrier
    sim = c.post("/voice/campaigns", json={
        "name": f"v76 sim drop {tag}", "agent_id": agent["id"], "endpoint_id": ep_id,
        "targets": [{"address": "+15552220003"}],
        "config": {"amd": {"mode": "greeting_end", "on_machine": "voicemail_drop",
                           "voicemail_message": "simulated message"}}}).json()
    c.post(f"/voice/campaigns/{sim['id']}/start", json={})
    detail = c.get(f"/voice/campaigns/{sim['id']}").json()
    sim_res = c.post(f"/voice/campaigns/{sim['id']}/targets/"
                     f"{detail['targets'][0]['id']}/simulate-answer",
                     json={"as_machine": "greeting_end"}).json()
    assert sim_res["simulated"] and sim_res["amd"]["drop"] is True, sim_res
    assert sim_res["amd"]["drop_record"]["message"] == "simulated message", sim_res
    assert sim_res["amd"]["session_end_reason"] == "voicemail_drop", sim_res
    return {"speak_commands": len(speaks), "hangup_commands": len(hangups),
            "drop_events": len(drops), "simulate": sim_res["amd"]["target_status"]}


# ---------------------------------------------------------------------------
# 3) room chat + the moderator's speaking queue
# ---------------------------------------------------------------------------

def chat_and_hand_check(c: httpx.Client, tag: str) -> dict:
    agent = c.post("/voice/agents", json={
        "name": f"Standup persona {tag}", "greeting_text": "Good morning.",
        "scaffold_handler": True}).json()
    meeting = c.post("/voice/meetings", json={
        "title": f"v76 standup {tag}", "agent_id": agent["id"]}).json()
    legs = []
    for label in ("alice", "bob"):
        join = c.post(f"/voice/meetings/{meeting['id']}/join",
                      json={"label": label, "channel": "web"})
        assert join.status_code == 200, join.text
        legs.append(join.json()["participant"])

    # the moderator posts, then a member posts
    res = c.post(f"/voice/meetings/{meeting['id']}/chat",
                 json={"text": "quick agenda check", "author": "Mo"})
    assert res.status_code == 200, res.text
    assert res.json()["message"]["role"] == "moderator"
    res = c.post(f"/voice/meetings/{meeting['id']}/chat",
                 json={"text": f"what is on the agenda? ({tag})",
                       "participant_id": legs[0]["id"], "ask_agent": True})
    assert res.status_code == 200, res.text
    reply = res.json().get("agent_reply")
    assert reply and reply["role"] == "agent", res.json()
    log = c.get(f"/voice/meetings/{meeting['id']}/chat").json()["messages"]
    assert log[-1]["role"] == "agent", log
    # the chat ALSO rode the member's leg conversation (one transcript)
    conv = c.get(f"/interactions/conversations/{legs[0]['session_id']}").json() \
        if False else None
    sess = c.get(f"/voice/sessions/{legs[0]['session_id']}").json()
    conv_id = sess["conversation_id"]
    msgs = c.get(f"/interactions/conversations/{conv_id}").json()
    chat_ride = [m for m in msgs.get("messages", []) if m.get("channel") == "meeting_chat"]
    assert chat_ride, "the chat landed on the leg's conversation"

    # hands: alice then bob; call next grants the FLOOR to alice
    c.post(f"/voice/meetings/{meeting['id']}/hand",
           json={"participant_id": legs[0]["id"], "note": "on the roadmap"})
    c.post(f"/voice/meetings/{meeting['id']}/hand", json={"participant_id": legs[1]["id"]})
    hq = c.get(f"/voice/meetings/{meeting['id']}/hand").json()["hand_queue"]
    assert [e["label"] for e in hq["entries"]] == ["alice", "bob"], hq
    nxt = c.post(f"/voice/meetings/{meeting['id']}/hand/next", json={}).json()
    assert nxt["called"] == legs[0]["id"], nxt
    assert nxt["floor"]["mode"] == "directed" and nxt["floor"]["label"] == "alice", nxt["floor"]
    assert nxt["hand_queue"]["count"] == 1, nxt["hand_queue"]
    # a directed-floor turn gate: bob's audio no longer triggers turns
    turn = c.post(f"/voice/sessions/{legs[1]['session_id']}/turn",
                  json={"transcript": "bob tries to interject", "confidence": 0.9}).json()
    assert turn["gated"] is True and turn["reason"] == "floor", turn
    # bob lowers his hand
    low = c.delete(f"/voice/meetings/{meeting['id']}/hand/{legs[1]['id']}")
    assert low.status_code == 200 and low.json()["hand_queue"]["count"] == 0, low.text
    return {"chat_messages": len(log), "floor_granted": nxt["floor"]["label"],
            "gated_reason": turn["reason"]}


def main() -> int:
    priv, pub_pem = _ed25519_keypair()
    db_path = f"{BACKEND}/data/smoke_v76_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.76.0", version
            tag = uuid.uuid4().hex[:6]

            q = channel_queue_check(c, tag)
            print(f"[1] CHANNEL QUEUE OK - a live call waited in the line (held, "
                  f"on_hold) with a derived FIFO position, and seating the head "
                  f"released it and attached the SAME call to the destination "
                  f"meeting as a joined leg (agent bound); queue now holds "
                  f"{q['waiting_after']} waiting, the empty line refuses honestly")

            d = voicemail_drop_check(c, tag, priv, pub_pem)
            print(f"[2] VOICEMAIL DROP OK - the campaign dialed a REAL number with "
                  f"machine_detection='greeting_end' riding the dial; the carrier's "
                  f"SIGNED greeting_ended webhook walked the session in_progress -> "
                  f"voicemail -> ended (voicemail_drop) while the stub captured the "
                  f"speak command carrying the configured message "
                  f"({d['speak_commands']} speak + {d['hangup_commands']} hangup) and "
                  f"the timeline carries {d['drop_events']} drop utterance(s); the "
                  f"target books meta.voicemail_drop and the simulate door walks the "
                  f"same drop (target {d['simulate']})")

            ch = chat_and_hand_check(c, tag)
            print(f"[3] ROOM CHAT + MODERATOR QUEUE OK - {ch['chat_messages']} chat "
                  f"messages (moderator + member + the agent's reply on the member's "
                  f"leg conversation), two hands raised, calling next granted the "
                  f"floor to {ch['floor_granted']} through the directed-floor "
                  f"primitive (a non-holder's turn gated: {ch['gated_reason']}), and "
                  f"the last hand lowered")

            print(f"\nALL 3 CHECKS GREEN - v76 live smoke passed (version {version})")
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
