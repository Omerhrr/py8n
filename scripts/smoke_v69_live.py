"""V69 live smoke: boot the real server and verify the three fronts.

1. REAL PROVIDER ADAPTERS: a Telegram endpoint and a WhatsApp (Meta Cloud
   API) endpoint receive their NATIVE webhooks - the Bot API secret header
   and Meta's X-Hub-Signature-256 HMAC are verified, the GET verification
   handshake echoes the challenge, the handler workflow answers, the reply
   is recorded in the interaction transcript and the outbound delivery is
   honestly skipped while no bot credential is configured.
2. VOICE PRIMITIVES: a call opens (linked to an interaction conversation),
   the state machine runs ringing -> answered -> turns, barge-in cancels
   the active TTS utterance and is counted, hangup ends the call, and a
   Twilio call-status callback drives a second (outbound) session.
3. RATE SHAPING / QUOTAS ON SERVING TOKENS: a token with rate_per_min=2
   shapes the 3rd serving call with 429 + Retry-After + X-RateLimit-*,
   a daily quota exhausts with the UTC reset named, an unlimited token
   keeps serving, usage is readable, and the SSE stream enforces the same.

Usage: /home/z/.venv/bin/python scripts/smoke_v69_live.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
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
    res = c.post("/workflows", json={"name": f"v69-handler-{tag}", "graph": {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "reply", "type": "code", "name": "reply", "position": {"x": 1, "y": 0}, "parameters": {"code": code}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "reply"}],
    }})
    assert res.status_code in (200, 201), res.text
    return res.json()


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in raw.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        event, data = "message", None
        for line in frame.split("\n"):
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if data is not None:
            events.append((event, json.loads(data)))
    return events


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v69_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
    })
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8199", "--log-level", "warning"],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=API, timeout=600) as c:
            wait_health(c)
            tag = uuid.uuid4().hex[:6]

            # ================= 1) real provider adapters ====================
            handler = _echo_handler(c, tag)

            res = c.post("/channels/endpoints", json={
                "name": f"tg-{tag}", "provider": "telegram_bot_api",
                "handler_workflow_id": handler["id"],
                "config": {"secret_token": f"tgsec-{tag}", "bot_token": ""}})
            assert res.status_code == 201, res.text
            tg = res.json()
            assert tg["webhook_url"].startswith("/api/v1/channels/telegram/")
            tg_path = tg["webhook_url"].replace("/api/v1", "", 1)

            res = c.post(tg_path, json={"update_id": 1, "message": {
                "message_id": 1, "from": {"id": 42, "first_name": "Ada"},
                "chat": {"id": 42, "type": "private"}, "text": "order a laptop"}},
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
            assert res.status_code == 401, res.status_code
            res = c.post(tg_path, json={"update_id": 1, "message": {
                "message_id": 1, "from": {"id": 42, "first_name": "Ada"},
                "chat": {"id": 42, "type": "private"}, "text": "order a laptop"}},
                headers={"X-Telegram-Bot-Api-Secret-Token": f"tgsec-{tag}"})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["ok"] is True and body["received"] == 1
            handled = body["handled"][0]
            assert handled["reply"] == "Hi Ada, got: order a laptop", handled
            assert handled["delivery"] == "skipped" and "bot_token" in handled["detail"]

            wa_res = c.post("/channels/endpoints", json={
                "name": f"wa-{tag}", "provider": "meta_cloud_api",
                "handler_workflow_id": handler["id"],
                "config": {"verify_token": f"vt-{tag}", "app_secret": f"appsec-{tag}",
                           "phone_number_id": "PN1", "access_token": ""}})
            assert wa_res.status_code == 201, wa_res.text
            wa = wa_res.json()
            wa_path = wa["webhook_url"].replace("/api/v1", "", 1)

            res = c.get(wa_path, params={"hub.mode": "subscribe", "hub.verify_token": f"vt-{tag}",
                                         "hub.challenge": "1158201444"})
            assert res.status_code == 200 and res.text == "1158201444", res.text
            wa_payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": "PN1"},
                "contacts": [{"wa_id": "234801", "profile": {"name": "Grace"}}],
                "messages": [{"from": "234801", "id": "wamid.9", "type": "text",
                              "text": {"body": "refill the order"}}]}}]}]}
            raw = json.dumps(wa_payload).encode()
            sig = "sha256=" + hmac.new(f"appsec-{tag}".encode(), raw, hashlib.sha256).hexdigest()
            res = c.post(wa_path, content=raw, headers={"X-Hub-Signature-256": sig})
            assert res.status_code == 200, res.text
            handled = res.json()["handled"][0]
            assert handled["reply"] == "Hi Grace, got: refill the order", handled

            # secrets never echo back
            listing = json.dumps(c.get("/channels/endpoints").json())
            assert f"appsec-{tag}" not in listing and f"tgsec-{tag}" not in listing

            conv = c.get(f"/interactions/conversations/{handled['conversation_id']}").json()
            roles = [m["role"] for m in conv["messages"] if m["role"] in ("user", "agent")]
            assert roles == ["user", "agent"]
            user_msg = next(m for m in conv["messages"] if m["role"] == "user")
            assert user_msg["payload"]["provider"] == "meta_cloud_api"
            print(f"[1] REAL PROVIDER ADAPTERS OK - telegram secret-verified webhook answered "
                  f"({handled['reply'][:30]}...), meta HMAC verified + handshake echoed, "
                  f"delivery honestly skipped, transcript provider-stamped")

            # ================= 2) voice primitives ==========================
            res = c.post("/voice/sessions", json={
                "direction": "inbound", "provider": "twilio", "call_ref": f"CA-{tag}",
                "from_ref": "+234-803", "to_ref": "+234-900", "handler_workflow_id": handler["id"]})
            assert res.status_code == 201, res.text
            sess = res.json()
            assert sess["state"] == "initiated" and sess["conversation_id"]

            res = c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.ringing"})
            assert res.json()["state"] == "ringing"
            res = c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.answered"})
            assert res.json()["state"] == "in_progress"

            res = c.post(f"/voice/sessions/{sess['id']}/turn",
                         json={"transcript": "I want to order a laptop", "confidence": 0.93})
            assert res.status_code == 200, res.text
            turn = res.json()
            assert turn["reply"] == "Hi , got: I want to order a laptop", turn
            assert turn["tts"]["provider"] == "openai_tts" and turn["tts"]["barge_in_ok"] is True
            assert turn["tts"]["tts_id"]

            res = c.post(f"/voice/sessions/{sess['id']}/barge-in")
            assert res.status_code == 200, res.text
            assert res.json()["interrupted"] == turn["tts"]["tts_id"]
            res = c.post(f"/voice/sessions/{sess['id']}/barge-in")
            assert res.status_code == 400 and "nothing is playing" in res.json()["detail"]

            res = c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "hangup"})
            assert res.json()["state"] == "ended"
            full = c.get(f"/voice/sessions/{sess['id']}").json()
            assert full["barge_in_count"] == 1 and full["turn_count"] == 1
            conv_v = c.get(f"/interactions/conversations/{sess['conversation_id']}").json()
            voice_msgs = [m for m in conv_v["messages"] if m["role"] in ("user", "agent")]
            assert [(m["role"], m["channel"]) for m in voice_msgs] == [("user", "voice"), ("agent", "voice")]

            # twilio call-status callback drives an outbound dial
            res = c.post("/voice/sessions", json={
                "direction": "outbound", "to_ref": "+234-803", "handler_workflow_id": handler["id"]})
            dial = res.json()
            for status, want in (("ringing", "ringing"), ("in-progress", "in_progress"), ("no-answer", "ended")):
                res = c.post(f"/voice/webhooks/twilio/{dial['id']}",
                             json={"CallSid": f"CA-dial-{tag}", "CallStatus": status})
                assert res.json()["state"] == want, (status, res.text)
            assert c.get(f"/voice/sessions/{dial['id']}").json()["end_reason"] == "no_answer"

            contracts = c.get("/voice/contracts").json()
            assert "openai_whisper" in contracts["asr"]["providers"] and "elevenlabs" in contracts["tts"]["providers"]
            print(f"[2] VOICE PRIMITIVES OK - state machine ringing->in_progress->ended, "
                  f"turn answered through the linked conversation, barge-in cancelled the "
                  f"TTS utterance, twilio callback drove outbound dial to no_answer")

            # ================= 3) rate shaping / quotas =====================
            run = _train_lm(c, f"smoke69-lm-{tag}")
            assert run["status"] == "success", str(run.get("error"))[:400]
            res = c.post("/deployments", json={"name": f"lim ep {tag}", "model": f"smoke69-lm-{tag}"})
            assert res.status_code == 201, res.text
            dep = res.json()
            wf_id = dep["workflow"]["id"]

            res = c.post(f"/deployments/{dep['id']}/tokens", json={"name": "shaped", "rate_per_min": 2})
            shaped = res.json()
            scall = {"Authorization": f"Bearer {shaped['token']}"}
            for i in range(2):
                res = c.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"}, headers=scall)
                assert res.status_code == 200, res.text
            res = c.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"}, headers=scall)
            assert res.status_code == 429, res.status_code
            assert "rate limit exceeded" in res.json()["detail"]
            assert int(res.headers["Retry-After"]) >= 1 and res.headers["X-RateLimit-Limit"] == "2"

            res = c.post(f"/deployments/{dep['id']}/tokens", json={"name": "quotal", "daily_quota": 2})
            quotal = res.json()
            qcall = {"X-Deployment-Token": quotal["token"]}
            for i in range(2):
                res = c.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"}, headers=qcall)
                assert res.status_code == 200, res.text
            res = c.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"}, headers=qcall)
            assert res.status_code == 429 and "daily quota exhausted" in res.json()["detail"]
            assert "T00:00:00" in res.headers["X-Quota-Reset"]

            res = c.post(f"/deployments/{dep['id']}/tokens", json={"name": "free"})
            free = res.json()
            fcall = {"Authorization": f"Bearer {free['token']}"}
            for i in range(3):
                res = c.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"}, headers=fcall)
                assert res.status_code == 200, res.text

            usage = c.get(f"/deployments/{dep['id']}/tokens/{quotal['id']}/usage").json()
            assert usage["usage"]["day_used"] == 2 and usage["usage"]["daily_quota"] == 2

            # the SSE stream enforces the same limits
            res = c.post(f"/deployments/{dep['id']}/tokens", json={"name": "streamer", "rate_per_min": 1})
            streamer = res.json()
            stcall = {"Authorization": f"Bearer {streamer['token']}"}
            res = c.post(f"/deployments/{dep['id']}/stream", headers=stcall,
                         json={"prompt": "the agent", "max_tokens": 4})
            assert res.status_code == 200, res.text
            events = _parse_sse(res.text)
            assert events[-1][0] == "done" and events[-1][1]["tokens_generated"] == 4
            res = c.post(f"/deployments/{dep['id']}/stream", headers=stcall,
                         json={"prompt": "the agent", "max_tokens": 4})
            assert res.status_code == 429 and "rate limit exceeded" in res.json()["detail"]

            print(f"[3] RATE SHAPING / QUOTAS OK - rate_per_min=2 shaped the 3rd call "
                  f"(429 + Retry-After + X-RateLimit-*), daily quota exhausted with UTC reset, "
                  f"unlimited token unaffected, usage counters live, SSE stream gated too")

            print(f"\nALL 3 CHECKS GREEN - v69 live smoke passed "
                  f"(version {c.get('/health').json().get('version', '?')})")
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
