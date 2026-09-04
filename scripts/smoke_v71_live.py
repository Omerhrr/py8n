"""V71 live smoke: boot the real server and verify both fronts.

1. EMAIL CHANNEL: a signed-JSON mail webhook (X-Py8n-Signature HMAC) is
   received by the REAL server - the reply is recorded in the transcript
   and delivery is honestly skipped without SMTP credentials (subject
   threads as "Re:"); a raw-MIME urlencoded webhook (SendGrid inbound
   parse shape) rides the SAME receiver and the MIME is parsed
   server-side; tampered signatures refuse with 403.
2. SMS CHANNELS: the any-gateway contract (generic_sms) answers a signed
   {from, to, text} webhook honestly; telnyx_sms receives an RFC 9421
   signed message.received through the SAME signatures as voice (a
   delivery status is an honest skip; a mangled signature 401s).
3. VOICE AGENT: the builder creates an agent with a SCAFFOLDED handler
   (which runs OFFLINE through the run endpoint), a Telnyx endpoint
   bound to the agent answers a SIP call - the greeting is spoken
   (speak built from the agent's own TTS config) - and a REAL websocket
   media stream hears the caller: barge-in cancels the greeting, the
   AGENT's ASR engine transcribes, the turn answers with the agent's
   persona and TTS voice, and the transcript lands in ONE conversation.

Usage: /home/z/.venv/bin/python scripts/smoke_v71_live.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
import uuid
from email.message import EmailMessage
from urllib.parse import urlencode

import httpx
import websockets

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v71_server.py"
API = "http://127.0.0.1:8199/api/v1"


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


import struct  # noqa: E402


def _sign(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _echo_handler(c: httpx.Client, tag: str) -> dict:
    code = (
        "env = input_data.get('payload', {})\n"
        "p = env.get('participant') or {}\n"
        "result = {'text': 'Hi ' + str(p.get('name', '')) + ', got: ' + str(env.get('text', ''))}\n"
    )
    res = c.post("/workflows", json={"name": f"v71-handler-{tag}", "graph": {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "reply", "type": "code", "name": "reply", "position": {"x": 1, "y": 0}, "parameters": {"code": code}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "reply"}],
    }})
    assert res.status_code in (200, 201), res.text
    return res.json()


def _ed25519_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub_pem


def _rfc9421_headers(priv, raw: bytes, target: str) -> dict:
    components = ("@method", "@target", "content-digest")
    covered = " ".join(f'"{c}"' for c in components)
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode()
    lines = ['"@method": POST', f'"@target": {target}',
             f'"content-digest": sha-256=:{digest}:']
    lines.append(f'"@signature-params": ({covered});created=1618884473;keyid="k1"')
    sig = base64.b64encode(priv.sign("\n".join(lines).encode("utf-8"))).decode()
    return {"signature-input": f'sig1=({covered});created=1618884473;keyid="k1"',
            "signature": f"sig1=:{sig}:",
            "content-digest": f"sha-256=:{digest}:"}


# ---------------------------------------------------------------------------
# 1) the email channel
# ---------------------------------------------------------------------------

def email_check(c: httpx.Client, tag: str) -> dict:
    handler = _echo_handler(c, f"email-{tag}")
    secret = f"mail-sekrit-{tag}"
    res = c.post("/channels/endpoints", json={
        "name": f"inbox-{tag}", "provider": "email_inbound",
        "handler_workflow_id": handler["id"], "config": {"secret": secret}})
    assert res.status_code == 201, res.text
    ep = res.json()
    assert ep["channel"] == "email"
    path = ep["webhook_url"].replace("/api/v1", "", 1)

    body = json.dumps({
        "from": {"address": f"ada-{tag}@lovelace.test", "name": "Ada Lovelace"},
        "to": "support@py8n.test", "subject": "Engine trouble",
        "text": "My analytical engine stopped.", "message_id": f"<m1-{tag}@x>"})
    res = c.post(path, content=body.encode(),
                 headers={"content-type": "application/json",
                          "x-py8n-signature": _sign(secret, body.encode())})
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["received"] == 1 and not out["skipped"], out
    handled = out["handled"][0]
    assert handled["reply"] == "Hi Ada Lovelace, got: My analytical engine stopped.", handled
    assert handled["delivery"] == "skipped" and "smtp_host" in handled["detail"], handled
    assert handled["request"]["transport"] == "smtp"
    assert handled["request"]["subject"] == "Re: Engine trouble"
    conv_id = handled["conversation_id"]

    # the raw-MIME urlencoded shape rides the SAME receiver
    mime = EmailMessage()
    mime["From"] = f"ada-{tag}@lovelace.test"
    mime["To"] = "support@py8n.test"
    mime["Subject"] = "Re: Engine trouble"
    mime.set_content("Any update?")
    form_body = urlencode({"email": mime.as_string(), "envelope": "{}"}).encode()
    res = c.post(path, content=form_body,
                 headers={"content-type": "application/x-www-form-urlencoded",
                          "x-py8n-signature": _sign(secret, form_body)})
    assert res.status_code == 200, res.text
    out2 = res.json()
    assert out2["received"] == 1 and out2["handled"][0]["text"].strip() == "Any update?", out2
    assert out2["handled"][0]["conversation_id"] == conv_id  # one thread per sender

    conv = c.get(f"/interactions/conversations/{conv_id}").json()
    assert conv["channel"] == "email" and conv["message_count"] >= 5  # system + 2x(user+agent)

    # tampered signature -> 403 before anything runs
    evil = b'{"from": "e@x.test", "text": "evil"}'
    res = c.post(path, content=evil, headers={"content-type": "application/json",
                                              "x-py8n-signature": _sign("wrong", evil)})
    assert res.status_code == 403, res.text
    return {"conversation": conv_id, "reply": handled["reply"], "subject": handled["request"]["subject"]}


# ---------------------------------------------------------------------------
# 2) the SMS channels
# ---------------------------------------------------------------------------

def sms_check(c: httpx.Client, tag: str) -> dict:
    handler = _echo_handler(c, f"sms-{tag}")

    # ---- generic_sms: the any-gateway HMAC contract --------------------
    secret = f"sms-sekrit-{tag}"
    res = c.post("/channels/endpoints", json={
        "name": f"gw-{tag}", "provider": "generic_sms",
        "handler_workflow_id": handler["id"], "config": {"secret": secret}})
    assert res.status_code == 201, res.text
    gw = res.json()
    assert gw["channel"] == "sms"
    path = gw["webhook_url"].replace("/api/v1", "", 1)
    body = json.dumps({"from": "+234801", "to": "PY8N", "text": "Where is my order?",
                       "id": f"sms-{tag}"})
    res = c.post(path, content=body.encode(),
                 headers={"content-type": "application/json",
                          "x-py8n-signature": _sign(secret, body.encode())})
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["handled"][0]["reply"] == "Hi , got: Where is my order?", out
    assert out["handled"][0]["delivery"] == "skipped" and "send_url" in out["handled"][0]["detail"], out
    # tampered -> 401
    res = c.post(path, content=body.encode(),
                 headers={"content-type": "application/json",
                          "x-py8n-signature": _sign("wrong", body.encode())})
    assert res.status_code == 401, res.text

    # ---- telnyx_sms: RFC 9421, the SAME signatures as voice -------------
    priv, pub_pem = _ed25519_keypair()
    res = c.post("/channels/endpoints", json={
        "name": f"tnx-sms-{tag}", "provider": "telnyx_sms",
        "handler_workflow_id": handler["id"], "config": {"public_key": pub_pem}})
    assert res.status_code == 201, res.text
    tnx = res.json()
    tnx_path = tnx["webhook_url"].replace("/api/v1", "", 1)

    def signed(payload: dict):
        raw = json.dumps(payload).encode()
        return raw, _rfc9421_headers(priv, raw, f"/api/v1{tnx_path}")

    raw, headers = signed({"data": {"event_type": "message.received", "payload": {
        "id": f"tm-{tag}", "from": {"phone_number": "+234803"},
        "to": [{"phone_number": "+234804"}], "text": "Reply HELP for help"}}})
    res = c.post(tnx_path, content=raw, headers=headers)
    assert res.status_code == 200, res.text
    out2 = res.json()
    assert out2["handled"][0]["reply"] == "Hi , got: Reply HELP for help", out2
    assert out2["handled"][0]["request"]["json"]["to"] == "+234803"

    # a delivery status rides the SAME signed receiver as an honest skip
    raw, headers = signed({"data": {"event_type": "message.finalized", "payload": {"id": "df"}}})
    res = c.post(tnx_path, content=raw, headers=headers)
    assert res.status_code == 200 and res.json()["skipped"][0]["reason"] == "status_update", res.text

    # mangled signature -> 401
    raw, headers = signed({"data": {"event_type": "message.received", "payload": {"text": "x"}}})
    headers["signature"] = "sig1=:bmls:"
    assert c.post(tnx_path, content=raw, headers=headers).status_code == 401

    return {"gateway_reply": out["handled"][0]["reply"], "telnyx_reply": out2["handled"][0]["reply"]}


# ---------------------------------------------------------------------------
# 3) the voice agent
# ---------------------------------------------------------------------------

def _pcm(samples: list[int]) -> str:
    return base64.b64encode(struct.pack(f"<{len(samples)}h", *samples)).decode()


SILENT_200MS = _pcm([0] * 1600)
LOUD_200MS = _pcm([9000] * 1600)


def _media(payload: str, chunk: int) -> str:
    return json.dumps({"event": "media", "media": {
        "payload": payload, "track": "inbound", "chunk": chunk,
        "encoding": "linear16", "sample_rate": 8000}})


def voice_agent_check(c: httpx.Client, tag: str) -> dict:
    # ---- the agent: scaffolded handler runs OFFLINE ----------------------
    res = c.post("/voice/agents", json={
        "name": f"Front Desk {tag}", "greeting_text": "Hello, you have reached the front desk.",
        "asr_provider": "py8n_local", "tts_provider": "piper_local", "tts_voice": "en_US-amy",
        "system_prompt": "You are the polite front desk agent.", "scaffold_handler": True})
    assert res.status_code == 201, res.text
    agent = res.json()
    assert agent["handler_is_scaffold"] is True and agent["speech"]["asr_engine_registered"], agent

    res = c.post(f"/workflows/{agent['handler_workflow_id']}/run", json={"payload": {
        "text": "I need help", "metadata": {"system_prompt": agent["system_prompt"]}}})
    assert res.status_code == 200, res.text
    ex = res.json()["execution_id"]
    for _ in range(400):
        det = c.get(f"/executions/{ex}").json()
        if det["status"] not in ("running", "queued"):
            break
        time.sleep(0.05)
    assert det["status"] == "success", det
    reply = det["node_runs"][-1]["output"]
    reply_text = reply.get("result", reply)["text"] if isinstance(reply, dict) else str(reply)
    assert reply_text == "[You are the polite front desk agent] You said: I need help", reply_text

    # ---- a Telnyx endpoint BOUND TO THE AGENT answers a SIP call ---------
    priv, pub_pem = _ed25519_keypair()
    res = c.post("/channels/endpoints", json={
        "name": f"tnx-agent-{tag}", "provider": "telnyx_call_control",
        "config": {"public_key": pub_pem, "agent_id": agent["id"]}})
    assert res.status_code == 201, res.text
    tnx = res.json()
    tnx_path = tnx["webhook_url"].replace("/api/v1", "", 1)

    def signed(payload: dict):
        raw = json.dumps(payload).encode()
        return raw, _rfc9421_headers(priv, raw, f"/api/v1{tnx_path}")

    call_cc = f"cc-agent-{tag}"
    raw, headers = signed({"data": {"event_type": "call.initiated", "payload": {
        "call_control_id": call_cc, "call_session_id": f"cs-{tag}",
        "direction": "incoming", "from": "sip:caller@external.sip",
        "to": "sip:frontdesk@sip.telnyx.com"}}})
    res = c.post(tnx_path, content=raw, headers=headers)
    assert res.status_code == 200, res.text
    handled = res.json()["handled"][0]
    assert handled["created"] is True and handled["state"] == "ringing", handled
    session_id = handled["session_id"]

    raw, headers = signed({"data": {"event_type": "call.answered", "payload": {
        "call_control_id": call_cc}}})
    res = c.post(tnx_path, content=raw, headers=headers)
    assert res.status_code == 200, res.text
    answered = res.json()["handled"][0]
    assert answered["state"] == "in_progress" and "greeting_speak_built" in answered["actions"], answered
    assert answered["delivery"]["request"]["json"]["payload"] == "Hello, you have reached the front desk.", answered

    sess = c.get(f"/voice/sessions/{session_id}").json()
    assert sess["agent"]["voice_agent_id"] == agent["id"], sess
    assert sess["handler_workflow_id"] == agent["handler_workflow_id"]
    assert sess["active_tts"] is True  # the greeting is playing

    # ---- the media stream: barge-in over the greeting, agent ASR ---------
    async def _stream():
        turn_reply = ""
        uri = f"ws://127.0.0.1:8199/api/v1/voice/sessions/{session_id}/media"
        async with websockets.connect(uri, open_timeout=10) as ws:
            connected = json.loads(await ws.recv())
            assert connected["agent"]["id"] == agent["id"], connected
            assert connected["asr_engine"] == "py8n_local" and connected["asr_engine_registered"], connected
            await ws.send(json.dumps({"event": "start", "start": {
                "streamSid": f"SS{tag}", "callSid": call_cc,
                "customParameters": {"encoding": "linear16", "sample_rate": 8000}}}))
            ack = json.loads(await ws.recv())
            assert ack["event"] == "stream_started", ack

            # the caller SPEAKS OVER THE GREETING
            await ws.send(_media(LOUD_200MS, 1))
            ev = json.loads(await ws.recv())
            assert ev["event"] == "barge_in" and ev["interrupted"], ev
            ev = json.loads(await ws.recv())
            assert ev["event"] == "speech.started", ev

            for i in (2, 3, 4):
                await ws.send(_media(SILENT_200MS, i))
            ev = json.loads(await ws.recv())
            assert ev["event"] == "speech.ended", ev
            ev = json.loads(await ws.recv())
            assert ev["event"] == "asr.final", ev
            assert ev["asr"]["transcript"] == "I want to order a laptop", ev
            ev = json.loads(await ws.recv())
            assert ev["event"] == "turn", ev
            turn_reply = ev["reply"]
            assert turn_reply == "[You are the polite front desk agent] You said: I want to order a laptop", ev
            assert ev["tts"]["provider"] == "piper_local" and ev["tts"]["voice"] == "en_US-amy", ev

            await ws.send(json.dumps({"event": "stop"}))
            ev = json.loads(await ws.recv())
            assert ev["event"] == "stream_stopped" and ev["stats"]["chunks"] == 4, ev
        return ev["stats"], turn_reply

    stats, turn_reply = asyncio.run(_stream())

    sess = c.get(f"/voice/sessions/{session_id}").json()
    assert sess["turn_count"] == 1 and sess["barge_in_count"] == 1, sess
    kinds = [e["kind"] for e in sess["events"]]
    assert "tts.started" in kinds and "barge_in" in kinds and "asr.final" in kinds, kinds
    conv = c.get(f"/interactions/conversations/{sess['conversation_id']}").json()
    msgs = [(m["role"], m["channel"]) for m in conv["messages"] if m["role"] in ("user", "agent")]
    assert msgs == [("user", "voice"), ("agent", "voice")], msgs
    return {"session": session_id, "chunks": stats["chunks"], "turn_reply": turn_reply}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v71_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PORT": "8199",
    })
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=API, timeout=600) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.71.0", version
            tag = uuid.uuid4().hex[:6]

            email = email_check(c, tag)
            print(f"[1] EMAIL CHANNEL OK - signed JSON webhook answered ('{email['reply']}'), "
                  f"reply threads '{email['subject']}' over SMTP (honestly skipped without "
                  f"credentials); raw-MIME urlencoded ride the SAME receiver into ONE thread; "
                  f"tampered signature 403")

            sms = sms_check(c, tag)
            print(f"[2] SMS CHANNELS OK - any-gateway contract answered ('{sms['gateway_reply']}', "
                  f"send_url honestly skipped, tampered 401); Telnyx Messaging answered an RFC 9421 "
                  f"signed message.received ('{sms['telnyx_reply']}'), delivery status skipped, "
                  f"mangled signature 401")

            agent = voice_agent_check(c, tag)
            print(f"[3] VOICE AGENT OK - scaffolded handler ran offline; a Telnyx endpoint bound "
                  f"to the agent answered a SIP call and the greeting was spoken from the agent's "
                  f"own TTS config; a REAL websocket stream heard the caller: barge-in cancelled "
                  f"the greeting, the agent's ASR engine transcribed and the turn answered with "
                  f"the agent's persona and voice ({agent['turn_reply']})")

            print(f"\nALL 3 CHECKS GREEN - v71 live smoke passed (version {version})")
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


if __name__ == "__main__":
    raise SystemExit(main())
