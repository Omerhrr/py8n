"""V70 live smoke: boot the real server and verify the three fronts.

1. VOICE AUDIO TRANSPORT (media streams / websocket ASR): a REAL websocket
   to the running uvicorn carries the provider media dialect (start/media/
   mark/stop, base64 linear16 @ 8kHz); py8n decodes the audio, RMS-VAD
   segments it into utterances, the registered ASR engine transcribes, the
   SAME voice turn answers through the linked conversation, barge-in fires
   when the caller speaks over the active utterance, and the stop frame
   returns honest counters that also land on the session detail.
2. MORE PROVIDERS: a Telnyx Call Control endpoint (SIP refs ride through)
   receives RFC 9421-signed webhooks - the call_control_id finds-or-creates
   the voice session, the gather digits run the SAME voice turn and the
   speak command is built (honestly skipped without an api_key), hangup
   ends the call; a WhatsApp (Meta Cloud API) endpoint receives a signed
   interactive button_reply tap, and interactive reply buttons build into
   the exact Graph API request while a non-meta provider refuses loudly.
3. CROSS-PROCESS LIMIT STORAGE: a token with rate_per_min=2 shapes the 3rd
   serving call (429 + Retry-After + X-RateLimit-*); the smoke process then
   opens the sqlite file DIRECTLY - a second process reading the first
   one's counters - and finds exactly the admitted rows, no row for the
   refusal; unlimited traffic records history and a policy applied later
   shapes it; the usage endpoint and the SSE stream read the same table.

Usage: /home/z/.venv/bin/python scripts/smoke_v70_live.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import struct
import subprocess
import time
import uuid

import httpx
import websockets

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v70_server.py"
API = "http://127.0.0.1:8199/api/v1"

CORPUS = [
    "the support agent resolved the ticket about the login issue",
    "the agent fixed the login bug and the customer left a review",
    "the customer asked about the refund policy for the order",
    "the agent shipped the order and closed the ticket today",
] * 8


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


def _run_wf(c: httpx.Client, name: str, graph: dict, payload: dict | None = None) -> dict:
    res = c.post("/workflows", json={"name": name, "graph": graph})
    assert res.status_code in (200, 201), res.text
    wf_id = res.json()["id"]
    res = c.post(f"/workflows/{wf_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    ex = res.json()["execution_id"]
    for _ in range(4000):
        det = c.get(f"/executions/{ex}").json()
        if det["status"] not in ("running", "queued"):
            return det
        time.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def _train_lm(c: httpx.Client, name: str) -> dict:
    return _run_wf(c, f"train-{name}", {"nodes": [
        {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
        {"id": "lm", "type": "lm_train", "name": "lm", "position": {"x": 1, "y": 0},
         "parameters": {"text_column": "doc", "d_model": 16, "epochs": 4, "model_name": name}},
    ], "edges": [{"id": "e1", "source": "t", "target": "lm"}]},
        {"items": [{"doc": d} for d in CORPUS]})


def _echo_handler(c: httpx.Client, tag: str) -> dict:
    code = (
        "env = input_data.get('payload', {})\n"
        "p = env.get('participant') or {}\n"
        "result = {'text': 'Hi ' + str(p.get('name', '')) + ', got: ' + str(env.get('text', ''))}\n"
    )
    res = c.post("/workflows", json={"name": f"v70-handler-{tag}", "graph": {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "reply", "type": "code", "name": "reply", "position": {"x": 1, "y": 0}, "parameters": {"code": code}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "reply"}],
    }})
    assert res.status_code in (200, 201), res.text
    return res.json()


# ---------------------------------------------------------------------------
# RFC 9421 signing for the telnyx receive path
# ---------------------------------------------------------------------------

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
# 1) the media stream, over a REAL websocket to the REAL server
# ---------------------------------------------------------------------------

def _pcm(samples: list[int]) -> str:
    return base64.b64encode(struct.pack(f"<{len(samples)}h", *samples)).decode()


SILENT_200MS = _pcm([0] * 1600)
LOUD_200MS = _pcm([9000] * 1600)


def _media(payload: str, chunk: int) -> str:
    return json.dumps({"event": "media", "media": {
        "payload": payload, "track": "inbound", "chunk": chunk,
        "encoding": "linear16", "sample_rate": 8000}})


async def media_stream_check(c: httpx.Client, tag: str) -> dict:
    handler = _echo_handler(c, tag)
    res = c.post("/voice/sessions", json={
        "direction": "inbound", "provider": "twilio", "call_ref": f"CAmedia-{tag}",
        "from_ref": "+234-700", "to_ref": "+234-701",
        "handler_workflow_id": handler["id"]})
    assert res.status_code == 201, res.text
    sess = res.json()
    assert res.json()["state"] == "initiated"
    res = c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.answered"})
    assert res.json()["state"] == "in_progress"

    uri = f"ws://127.0.0.1:8199/api/v1/voice/sessions/{sess['id']}/media"
    async with websockets.connect(uri, open_timeout=10) as ws:
        connected = json.loads(await ws.recv())
        assert connected["event"] == "connected" and connected["session_id"] == sess["id"], connected
        assert "py8n_local" in connected["asr_engines"], connected

        await ws.send(json.dumps({"event": "start", "start": {
            "streamSid": f"SS{tag}", "callSid": f"CAmedia-{tag}",
            "customParameters": {"encoding": "linear16", "sample_rate": 8000}}}))
        ack = json.loads(await ws.recv())
        assert ack["event"] == "stream_started", ack

        for i in range(3):
            await ws.send(_media(SILENT_200MS, i))
        await ws.send(_media(LOUD_200MS, 10))
        ev = json.loads(await ws.recv())
        assert ev["event"] == "speech.started", ev

        for i in (11, 12, 13):
            await ws.send(_media(SILENT_200MS, i))
        ev = json.loads(await ws.recv())
        assert ev["event"] == "speech.ended", ev
        assert ev["segment"]["duration_ms"] == 800.0, ev
        ev = json.loads(await ws.recv())
        assert ev["event"] == "asr.final", ev
        assert ev["asr"]["transcript"] == "I want to order a laptop"
        ev = json.loads(await ws.recv())
        assert ev["event"] == "turn", ev
        assert ev["reply"] == "Hi , got: I want to order a laptop", ev
        tts_id = ev["tts"]["tts_id"]

        # barge-in: the caller speaks over the active utterance
        await ws.send(_media(LOUD_200MS, 20))
        ev = json.loads(await ws.recv())
        assert ev["event"] == "barge_in" and ev["interrupted"] == tts_id, ev
        ev = json.loads(await ws.recv())
        assert ev["event"] == "speech.started", ev

        # junk frames skip honestly
        await ws.send(json.dumps({"event": "keepalive"}))
        ev = json.loads(await ws.recv())
        assert ev["event"] == "skipped" and ev["reason"] == "unknown_event", ev

        # stop: the final counters
        await ws.send(json.dumps({"event": "stop"}))
        ev = json.loads(await ws.recv())
        assert ev["event"] == "stream_stopped", ev
        stats = ev["stats"]
        assert stats["chunks"] == 8 and stats["skipped_frames"] == 1, stats

    detail = c.get(f"/voice/sessions/{sess['id']}").json()
    media = detail.get("media") or {}
    assert media.get("stopped") is True and media.get("stream_sid") == f"SS{tag}", media
    kinds = [e["kind"] for e in detail.get("events") or []]
    for kind in ("media.stream_started", "speech.started", "speech.ended",
                 "asr.final", "barge_in", "media.stream_stopped"):
        assert kind in kinds, (kind, kinds)
    conv = c.get(f"/interactions/conversations/{detail['conversation_id']}").json()
    msgs = [m for m in conv["messages"] if m["role"] in ("user", "agent")]
    assert [(m["role"], m["channel"]) for m in msgs] == [("user", "voice"), ("agent", "voice")]
    assert "order a laptop" in msgs[0]["text"]
    return {"session": sess["id"], "chunks": stats["chunks"], "audio_ms": stats["audio_ms"]}


# ---------------------------------------------------------------------------
# 2) telnyx (voice/SIP) + whatsapp interactive buttons
# ---------------------------------------------------------------------------

def providers_check(c: httpx.Client, tag: str) -> dict:
    handler = _echo_handler(c, tag)

    # ---- telnyx: RFC 9421-signed call control (SIP refs ride through) --
    priv, pub_pem = _ed25519_keypair()
    res = c.post("/channels/endpoints", json={
        "name": f"tnx-{tag}", "provider": "telnyx_call_control",
        "handler_workflow_id": handler["id"], "config": {"public_key": pub_pem}})
    assert res.status_code == 201, res.text
    tnx = res.json()
    tnx_path = tnx["webhook_url"].replace("/api/v1", "", 1)

    def signed(payload: dict):
        raw = json.dumps(payload).encode()
        return raw, _rfc9421_headers(priv, raw, f"/api/v1{tnx_path}")

    raw, headers = signed({"data": {"event_type": "call.initiated", "payload": {
        "call_control_id": f"cc-{tag}", "call_session_id": f"cs-{tag}",
        "direction": "incoming", "from": "sip:caller@external.sip",
        "to": "sip:agent@sip.telnyx.com"}}})
    res = c.post(tnx_path, content=raw, headers=headers)
    assert res.status_code == 200, res.text
    handled = res.json()["handled"][0]
    assert handled["created"] is True and handled["state"] == "ringing", handled
    assert "answer_built" in handled["actions"] and handled["delivery"]["delivery"] == "skipped"
    session_id = handled["session_id"]

    raw, headers = signed({"data": {"event_type": "call.answered", "payload": {
        "call_control_id": f"cc-{tag}"}}})
    assert c.post(tnx_path, content=raw, headers=headers).json()["handled"][0]["state"] == "in_progress"

    raw, headers = signed({"data": {"event_type": "call.gather.ended", "payload": {
        "call_control_id": f"cc-{tag}", "digits": "12"}}})
    handled = c.post(tnx_path, content=raw, headers=headers).json()["handled"][0]
    assert "turn_run" in handled["actions"] and "speak_built" in handled["actions"], handled
    assert handled["delivery"]["request"]["json"]["payload"] == "Hi , got: 12"

    raw, headers = signed({"data": {"event_type": "call.hangup", "payload": {
        "call_control_id": f"cc-{tag}", "hangup_cause": "NORMAL_CLEARING"}}})
    assert c.post(tnx_path, content=raw, headers=headers).json()["handled"][0]["state"] == "ended"

    sess = c.get(f"/voice/sessions/{session_id}").json()
    assert sess["state"] == "ended" and sess["end_reason"] == "hangup"
    assert sess["from"].startswith("sip:") and sess["provider"] == "telnyx"

    # unsigned webhook refuses
    raw, _ = signed({"data": {"event_type": "call.initiated", "payload": {"call_control_id": "x"}}})
    assert c.post(tnx_path, content=raw).status_code == 401

    # ---- whatsapp: the tap IS the message + interactive buttons --------
    res = c.post("/channels/endpoints", json={
        "name": f"wa-{tag}", "provider": "meta_cloud_api",
        "handler_workflow_id": handler["id"],
        "config": {"phone_number_id": "PN1", "verify_token": f"vt-{tag}",
                   "app_secret": f"appsec-{tag}"}})
    assert res.status_code == 201, res.text
    wa = res.json()
    wa_path = wa["webhook_url"].replace("/api/v1", "", 1)

    tap_msg = {"from": "234801", "id": f"w-{tag}", "type": "interactive",
               "interactive": {"type": "button_reply",
                               "button_reply": {"id": "sales", "title": "Talk to Sales"}}}
    tap_value = {"metadata": {"phone_number_id": "PN1"},
                 "contacts": [{"wa_id": "234801", "profile": {"name": "Grace"}}],
                 "messages": [tap_msg]}
    tap = {"object": "whatsapp_business_account",
           "entry": [{"changes": [{"value": tap_value}]}]}
    raw = json.dumps(tap).encode()
    sig = "sha256=" + hmac.new(f"appsec-{tag}".encode(), raw, hashlib.sha256).hexdigest()
    res = c.post(wa_path, content=raw, headers={"X-Hub-Signature-256": sig})
    assert res.status_code == 200, res.text
    assert res.json()["handled"][0]["reply"] == "Hi Grace, got: Talk to Sales"

    res = c.post(f"/channels/endpoints/{wa['id']}/preview-outbound",
                 json={"to": "234801", "text": "Pick a lane",
                       "buttons": [{"id": "sales", "title": "Talk to Sales"},
                                   {"id": "support", "title": "Support"}]})
    assert res.status_code == 200, res.text
    buttons = res.json()["json"]["interactive"]["action"]["buttons"]
    assert buttons[0] == {"type": "reply", "reply": {"id": "sales", "title": "Talk to Sales"}}

    # a 4th button violates Meta's documented limit -> 400
    res = c.post(f"/channels/endpoints/{wa['id']}/preview-outbound",
                 json={"to": "1", "text": "x",
                       "buttons": [{"id": str(i), "title": "t"} for i in range(4)]})
    assert res.status_code == 400 and "1..3 buttons" in res.json()["detail"]
    return {"telnyx_session": session_id, "sip_from": sess["from"],
            "turn_reply": "Hi , got: 12", "buttons": len(buttons)}


# ---------------------------------------------------------------------------
# 3) cross-process limit storage
# ---------------------------------------------------------------------------

def limits_check(c: httpx.Client, tag: str, db_path: str) -> dict:
    _train_lm(c, f"lim-{tag}")
    res = c.post("/deployments", json={"name": f"dep-{tag}",
                                       "model": f"lim-{tag}", "environment": "dev"})
    assert res.status_code == 201, res.text
    dep = res.json()
    wf_id = dep["workflow"]["id"]

    res = c.post(f"/deployments/{dep['id']}/tokens", json={"name": "shaped", "rate_per_min": 2})
    shaped = res.json()
    call = {"Authorization": f"Bearer {shaped['token']}"}
    for i in range(2):
        assert c.post(f"/webhooks/{wf_id}", json={"prompt": "hi"}, headers=call).status_code == 200
    res = c.post(f"/webhooks/{wf_id}", json={"prompt": "hi"}, headers=call)
    assert res.status_code == 429 and "rate limit exceeded" in res.json()["detail"]
    assert res.headers["X-RateLimit-Remaining"] == "0"

    # THE cross-process proof: this smoke process opens the sqlite file
    # DIRECTLY and reads the SERVER process's counters from the shared table
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT COUNT(*) FROM deployment_token_hits WHERE token_id = ?",
        (shaped["id"],)).fetchone()[0]
    con.close()
    assert rows == 2, f"expected exactly 2 shared hit rows, got {rows}"

    usage = c.get(f"/deployments/{dep['id']}/tokens/{shaped['id']}/usage").json()
    assert usage["usage"]["minute_used"] == 2 and usage["usage"]["minute_remaining"] == 0

    # unlimited traffic records history; a policy applied NOW shapes it
    res = c.post(f"/deployments/{dep['id']}/tokens", json={"name": "free"})
    free = res.json()
    fcall = {"Authorization": f"Bearer {free['token']}"}
    for _ in range(3):
        assert c.post(f"/webhooks/{wf_id}", json={"prompt": "hi"}, headers=fcall).status_code == 200
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT COUNT(*) FROM deployment_token_hits WHERE token_id = ?",
        (free["id"],)).fetchone()[0]
    con.close()
    assert rows == 3, rows
    res = c.put(f"/deployments/{dep['id']}/tokens/{free['id']}/limits",
                json={"rate_per_min": 1})
    assert res.status_code == 200 and res.json()["usage"]["minute_used"] == 3
    assert c.post(f"/webhooks/{wf_id}", json={"prompt": "hi"}, headers=fcall).status_code == 429
    c.put(f"/deployments/{dep['id']}/tokens/{free['id']}/limits",
          json={"rate_per_min": None, "daily_quota": None})

    # the SSE stream writes the same shared table
    res = c.post(f"/deployments/{dep['id']}/tokens", json={"name": "streamer", "rate_per_min": 1})
    streamer = res.json()
    scall = {"Authorization": f"Bearer {streamer['token']}"}
    res = c.post(f"/deployments/{dep['id']}/stream", headers=scall,
                 json={"prompt": "hi", "max_tokens": 4})
    assert res.status_code == 200, res.text
    res = c.post(f"/deployments/{dep['id']}/stream", headers=scall,
                 json={"prompt": "hi", "max_tokens": 4})
    assert res.status_code == 429 and "rate limit exceeded" in res.json()["detail"]
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT COUNT(*) FROM deployment_token_hits WHERE token_id = ?",
        (streamer["id"],)).fetchone()[0]
    con.close()
    assert rows == 1, rows  # the refusal never stored a row
    return {"shaped_rows": 2, "free_history": 3, "streamer_rows": 1}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v70_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.70.0", version
            tag = uuid.uuid4().hex[:6]

            media = asyncio.run(media_stream_check(c, tag))
            print(f"[1] MEDIA TRANSPORT OK - {media['chunks']} chunks / {media['audio_ms']}ms decoded "
                  f"over a real websocket; VAD segmented, ASR transcribed, the SAME voice turn "
                  f"answered, barge-in fired, honest counters on the session")

            providers = providers_check(c, tag)
            print(f"[2] PROVIDERS OK - Telnyx Call Control answered a SIP call "
                  f"({providers['sip_from']}) through the SAME voice primitives with the speak "
                  f"command built; WhatsApp button tap became the message and interactive buttons "
                  f"build the exact Graph API request (4th button refused)")

            limits = limits_check(c, tag, db_path)
            print(f"[3] CROSS-PROCESS LIMITS OK - rate 2/min shaped the 3rd call; ANOTHER PROCESS "
                  f"(this smoke) read the server's rows from the shared table: shaped="
                  f"{limits['shaped_rows']}, free-history={limits['free_history']}, streamer="
                  f"{limits['streamer_rows']} (refusals never stored); policy applied NOW shapes "
                  f"past traffic; SSE stream gated")

            print(f"\nALL 3 CHECKS GREEN - v70 live smoke passed (version {version})")
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
