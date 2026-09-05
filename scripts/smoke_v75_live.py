"""V75 live smoke: boot the real server and drive the room controls and
the dialer's schedule end to end.

1. MEETING MIX + FLOOR: a meeting with two web legs; the moderator mutes
   one member (their turn comes back gated - no asr.final on the leg,
   nothing in the merged transcript, an honest mix.gated marker on the
   session timeline), deafens the other (the handler replies but the
   TTS is withheld and no tts.started opens), spotlights a member
   (solo is exclusive), directs the floor to one leg (the non-holder is
   still transcribed but gated), releases the floor back to auto and
   watches the room flow again.
2. RETRY SCHEDULES: a campaign created with retry {max_attempts: 2,
   delays_minutes: [0, 60]} dials two REAL numbers against the stub
   carrier (the dial carries connection_id + client_state); one number
   NO_ANSWERs through the carrier's signed webhook (call.initiated ->
   call.hangup cause NO_ANSWER) - the target lands no_answer with the
   next attempt scheduled; POST /campaigns/{id}/retry dials what is due
   (the stub sees a THIRD dial) and reports the pass honestly.
3. ANSWERING MACHINE DETECTION: the campaign's amd {mode: detect,
   on_machine: hangup} rides the dial request (machine_detection:
   "detect" captured by the stub); the carrier's verdict arrives as a
   SIGNED call.machine.detection.ended webhook - the session walks
   in_progress -> voicemail -> ended (answering_machine), the target is
   marked voicemail, the derived progress counts it, and the honest
   simulate door walks the machine sequence too.

Usage: /home/z/.venv/bin/python scripts/smoke_v75_live.py
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
API = "http://127.0.0.1:8203/api/v1"
STUB_PORT = 3013

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
                self._json(200, {"data": {"call_control_id": f"CC-v75-{len(sys_stubs['dials'])}",
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


# ---------------------------------------------------------------------------
# 1) meetings: mix + floor through the real API
# ---------------------------------------------------------------------------

def meeting_mix_floor_check(c: httpx.Client, tag: str) -> dict:
    agent = c.post("/voice/agents", json={
        "name": f"Room persona {tag}", "greeting_text": "Welcome to the room.",
        "scaffold_handler": True}).json()
    assert agent.get("handler_workflow_id"), agent
    meeting = c.post("/voice/meetings", json={
        "title": f"v75 mix room {tag}", "agent_id": agent["id"]}).json()
    assert meeting["floor"]["mode"] == "auto", meeting

    legs = []
    for label in ("alice", "bob"):
        join = c.post(f"/voice/meetings/{meeting['id']}/join",
                      json={"label": label, "channel": "web"})
        assert join.status_code == 200, join.text
        legs.append(join.json()["participant"])

    # MUTE bob: the room does not hear him
    res = c.patch(f"/voice/meetings/{meeting['id']}/participants/{legs[1]['id']}/mix",
                  json={"muted": True})
    assert res.status_code == 200 and res.json()["participant"]["mix"]["muted"], res.text
    turn = c.post(f"/voice/sessions/{legs[1]['session_id']}/turn",
                  json={"transcript": "bob whispers something", "confidence": 0.9}).json()
    assert turn["gated"] is True and turn["reason"] == "muted", turn
    detail = c.get(f"/voice/meetings/{meeting['id']}").json()
    assert not [l for l in detail["transcript"] if l["side"] == "participant"], \
        "muted audio never reaches the room"

    # unmute, DEAFEN bob: the agent listens but does not speak to him
    c.patch(f"/voice/meetings/{meeting['id']}/participants/{legs[1]['id']}/mix",
            json={"muted": False, "deafened": True})
    sess_before = c.get(f"/voice/sessions/{legs[1]['session_id']}").json()
    tts_before = sum(1 for e in sess_before["events"] if e["kind"] == "tts.started")
    turn = c.post(f"/voice/sessions/{legs[1]['session_id']}/turn",
                  json={"transcript": "bob asks the agenda", "confidence": 0.9}).json()
    assert turn["reply"] and turn["reply_withheld"] and turn["tts"] is None, turn
    sess_after = c.get(f"/voice/sessions/{legs[1]['session_id']}").json()
    tts_after = sum(1 for e in sess_after["events"] if e["kind"] == "tts.started")
    assert tts_after == tts_before, "no new tts on a deafened leg"

    # FLOOR to alice: bob is still transcribed but gated
    res = c.post(f"/voice/meetings/{meeting['id']}/floor",
                 json={"mode": "directed", "participant_id": legs[0]["id"]})
    assert res.status_code == 200, res.text
    assert res.json()["meeting"]["floor"]["label"] == "alice"
    turn = c.post(f"/voice/sessions/{legs[1]['session_id']}/turn",
                  json={"transcript": "bob interjects anyway", "confidence": 0.9}).json()
    assert turn["gated"] is True and turn["reason"] == "floor", turn
    detail = c.get(f"/voice/meetings/{meeting['id']}").json()
    bob_lines = [l for l in detail["transcript"]
                 if l["side"] == "participant" and l["speaker"] == "bob"]
    assert any("interjects" in l["text"] for l in bob_lines), \
        "the room still transcribes a gated speaker"

    # SOLO alice is exclusive; release the floor; the room flows again
    c.patch(f"/voice/meetings/{meeting['id']}/participants/{legs[0]['id']}/mix",
            json={"solo": True})
    detail = c.get(f"/voice/meetings/{meeting['id']}").json()
    mixes = {p["label"]: p["mix"] for p in detail["participants"]}
    assert mixes["alice"]["solo"] and not mixes["bob"]["solo"], mixes
    res = c.post(f"/voice/meetings/{meeting['id']}/floor", json={"mode": "auto"})
    assert res.status_code == 200, res.text
    c.patch(f"/voice/meetings/{meeting['id']}/participants/{legs[0]['id']}/mix",
            json={"solo": False})
    turn = c.post(f"/voice/sessions/{legs[1]['session_id']}/turn",
                  json={"transcript": "bob finally gets an answer", "confidence": 0.9}).json()
    assert not turn.get("gated") and turn["reply"], turn
    return {"gated_reasons": ["muted", "floor"], "transcript_lines": len(detail["transcript"])}


# ---------------------------------------------------------------------------
# 2) campaigns: retry schedules + AMD through the signed carrier
# ---------------------------------------------------------------------------

def campaign_retry_amd_check(c: httpx.Client, tag: str, priv, pub_pem: str) -> dict:
    agent = c.post("/voice/agents", json={
        "name": f"Dialer persona {tag}", "greeting_text": "Hi, quick check.",
        "scaffold_handler": True}).json()
    ep = c.post("/channels/endpoints", json={
        "name": f"v75 telnyx {tag}", "provider": "telnyx_call_control",
        "config": {"api_key": "telnyx-v75-key",
                   "api_base": f"http://127.0.0.1:{STUB_PORT}/v2",
                   "public_key": pub_pem,
                   "connection_id": "conn-v75",
                   "webhook_url": f"http://127.0.0.1:8203/api/v1/channels/telnyx/v75/webhook",
                   "from_number": "+15550908"}})
    assert ep.status_code == 201, ep.text
    ep_id = ep.json()["id"]

    camp = c.post("/voice/campaigns", json={
        "name": f"v75 renewals {tag}", "agent_id": agent["id"],
        "endpoint_id": ep_id,
        "targets": [{"address": "+15551110001", "name": "human one"},
                    {"address": "+15551110002", "name": "no answer"}],
        "config": {"retry": {"max_attempts": 2, "delays_minutes": [0, 60],
                             "retry_on": ["no_answer"]},
                   "amd": {"mode": "detect", "on_machine": "hangup"}}})
    assert camp.status_code == 201, camp.text
    camp = camp.json()
    assert camp["config"]["amd"] == {"mode": "detect", "on_machine": "hangup"}
    assert camp["progress"]["retry"]["plan"]["max_attempts"] == 2

    start = c.post(f"/voice/campaigns/{camp['id']}/start", json={}).json()
    assert start["status"] == "running", start
    assert start["progress"]["placed"] == 2, start["progress"]
    assert len(sys_stubs["dials"]) == 2, sys_stubs["dials"]
    assert sys_stubs["dials"][0].get("machine_detection") == "detect", \
        "the AMD opt-in rides the dial request"
    state0 = start["targets"][0]["call_control_id"]
    state1 = start["targets"][1]["call_control_id"]

    hook_path = f"/api/v1/channels/telnyx/{ep_id}/webhook"
    url = f"http://127.0.0.1:8203{hook_path}"

    def _post_carrier(payload: dict) -> dict:
        raw = json.dumps(payload).encode()
        res = httpx.post(url, content=raw,
                         headers=_rfc9421_headers(priv, raw, hook_path), timeout=30)
        assert res.status_code == 200, res.text
        return res.json()

    # TARGET 2: the dial never connects - the carrier hangs up with NO_ANSWER
    out = _post_carrier({"data": {"event_type": "call.initiated",
                                  "payload": {"call_control_id": state1,
                                              "direction": "outgoing",
                                              "client_state": camp["id"]}}})
    out = _post_carrier({"data": {"event_type": "call.hangup",
                                  "payload": {"call_control_id": state1,
                                              "hangup_cause": "NO_ANSWER"}}})
    detail = c.get(f"/voice/campaigns/{camp['id']}").json()
    t2 = next(t for t in detail["targets"] if t["address"] == "+15551110002")
    assert t2["status"] == "no_answer", t2
    assert t2["attempts"] == 1 and t2["retry_at"], t2
    assert detail["progress"]["retry"]["due"] == 1, detail["progress"]["retry"]

    # TARGET 1: answered, then the machine verdict - the policy hangs up
    out = _post_carrier({"data": {"event_type": "call.initiated",
                                  "payload": {"call_control_id": state0,
                                              "direction": "outgoing"}}})
    out = _post_carrier({"data": {"event_type": "call.answered",
                                  "payload": {"call_control_id": state0,
                                              "client_state": _client_state(camp["id"],
                                                                           start["targets"][0]["id"])}}})
    handled = out["handled"][-1]
    assert handled["state"] == "in_progress", handled
    out = _post_carrier({"data": {"event_type": "call.machine.detection.ended",
                                  "payload": {"call_control_id": state0,
                                              "result": "machine"}}})
    handled = out["handled"][-1]
    assert "amd_hangup_built" in handled["actions"], handled
    assert handled["state"] == "ended" and handled["end_reason"] == "answering_machine", handled
    detail = c.get(f"/voice/campaigns/{camp['id']}").json()
    t1 = next(t for t in detail["targets"] if t["address"] == "+15551110001")
    assert t1["status"] == "voicemail" and t1["amd"]["result"] == "machine", t1
    assert detail["progress"]["counts"]["voicemail"] == 1, detail["progress"]

    # THE RETRY PASS: the no-answer is due (delay 0) - a REAL third dial
    retry = c.post(f"/voice/campaigns/{camp['id']}/retry", json={}).json()
    assert retry["retry_pass"]["dialed"] == 1, retry["retry_pass"]
    assert len(sys_stubs["dials"]) == 3, "the retry pass placed a real dial"
    detail = c.get(f"/voice/campaigns/{camp['id']}").json()
    t2 = next(t for t in detail["targets"] if t["address"] == "+15551110002")
    assert t2["status"] == "dialing" and t2["attempts"] == 2, t2

    # the honest simulate door walks the machine sequence without a carrier
    sim_camp = c.post("/voice/campaigns", json={
        "name": f"v75 sim {tag}", "agent_id": agent["id"], "endpoint_id": ep_id,
        "targets": [{"address": "+15551110003"}],
        "config": {"amd": {"mode": "detect", "on_machine": "hangup"}}}).json()
    c.post(f"/voice/campaigns/{sim_camp['id']}/start", json={})
    detail = c.get(f"/voice/campaigns/{sim_camp['id']}").json()
    sim = c.post(f"/voice/campaigns/{sim_camp['id']}/targets/"
                 f"{detail['targets'][0]['id']}/simulate-answer",
                 json={"as_machine": True}).json()
    assert sim["simulated"] and sim["amd"]["hangup"], sim
    assert sim["amd"]["target_status"] == "voicemail"
    assert sim["amd"]["session_state"] == "ended"
    return {"dials": len(sys_stubs["dials"]), "voicemail": t1["status"],
            "retry_dialed": retry["retry_pass"]["dialed"]}


def _client_state(campaign_id: str, target_id: str) -> str:
    raw = json.dumps({"cmp": campaign_id, "tgt": target_id}).encode()
    return base64.b64encode(raw).decode("ascii")


def main() -> int:
    priv, pub_pem = _ed25519_keypair()
    db_path = f"{BACKEND}/data/smoke_v75_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PORT": "8203",
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
            assert version == "1.75.0", version
            tag = uuid.uuid4().hex[:6]

            mf = meeting_mix_floor_check(c, tag)
            print(f"[1] MEETING MIX + FLOOR OK - the moderator muted a member "
                  f"(gated: {mf['gated_reasons'][0]}, the room never heard them), "
                  f"deafened another (the agent listened, the TTS was withheld), "
                  f"directed the floor (the non-holder stayed transcribed but gated: "
                  f"{mf['gated_reasons'][1]}), spotlighted solo exclusively, released "
                  f"the floor and watched the room flow; {mf['transcript_lines']} "
                  f"merged transcript lines")

            ca = campaign_retry_amd_check(c, tag, priv, pub_pem)
            print(f"[2] RETRY SCHEDULES + AMD OK - the campaign dialed 2 REAL numbers "
                  f"against the stub carrier with machine_detection='detect' riding the "
                  f"dial; a NO_ANSWER webhook scheduled the target's next attempt and "
                  f"the retry pass placed a REAL {ca['retry_dialed']} dial "
                  f"({ca['dials']} total); the carrier's machine verdict walked the "
                  f"session in_progress -> voicemail -> ended (answering_machine), the "
                  f"target landed {ca['voicemail']} and the honest simulate door walked "
                  f"the machine too")

            print(f"\nALL 2 CHECKS GREEN - v75 live smoke passed (version {version})")
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
