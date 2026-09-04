"""V69 feature tests: REAL provider adapters (Meta Cloud API, Telegram,
Discord - webhook-native), voice session primitives (call state machine,
barge-in, ASR/TTS contract), and rate-shaping/quotas on serving tokens.

- adapters: each provider's NATIVE dialect is parsed into the normalized
  interaction shape (multi-message Meta webhooks, Bot API updates, signed
  Discord interactions), non-messages are honestly skipped (delivery
  statuses, edits, stickers), verification is provider-native (X-Hub-
  Signature-256 HMAC, secret-token header, Ed25519), and the outbound
  request each send API expects is built exactly.
- channel endpoints: registering a provider connection produces a public
  webhook URL; posting a NATIVE webhook through it runs the whole path -
  verify -> parse -> interactions.ingest (handler answers) -> outbound
  delivery; secrets are masked in every API response; a Discord
  endpoint's configurable webhook_url lets the delivery test hit a REAL
  local HTTP receiver.
- voice: the call state machine refuses illegal transitions, voice turns
  run ASR-result -> handler -> TTS contract over the LINKED conversation
  (the voice transcript lives with every other channel), barge-in
  cancels the active utterance and is counted, Twilio call-status
  callbacks translate into session events.
- serving limits: a token with rate_per_min=2 rejects the 3rd request
  with 429 + Retry-After + X-RateLimit-* headers; a daily quota rejects
  with the UTC reset time; unlimited tokens stay unlimited; usage is
  readable; the SSE stream endpoint enforces the same limits.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v68).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.server
import json
import threading
import uuid

import httpx

from app.main import app
from app.services import executor as executor_mod
from app.services import serving_limits
from app.services import channel_adapters as adapters

API = "http://testserver/api/v1"

_CORPUS_SENTENCE = "the support agent resolved the ticket and the customer left a happy review"


def _corpus_rows(n: int = 14) -> list[dict]:
    return [{"doc": _CORPUS_SENTENCE + f" number {i} about the ticket and the agent"} for i in range(n)]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v69-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v69 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _run_and_wait(client: httpx.AsyncClient, wf_id: str, headers: dict, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{wf_id}/run", headers=headers, json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(400):
        res = await client.get(f"/executions/{exec_id}", headers=headers)
        assert res.status_code == 200, res.text
        if res.json()["status"] not in ("running", "queued"):
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


async def _train_lm(client: httpx.AsyncClient, tag: str, headers: dict, name: str) -> None:
    params = {"text_column": "doc", "d_model": 16, "epochs": 3, "model_name": name}
    res = await client.post("/workflows", headers=headers, json={"name": f"lm-{name}", "graph": {
        "nodes": [_node("t", "manual_trigger"), _node("lm", "lm_train", params)],
        "edges": [_edge("e1", "t", "lm")],
    }})
    wf = res.json()
    run = await _run_and_wait(client, wf["id"], headers, {"items": _corpus_rows()})
    assert run["status"] == "success", str(run.get("error"))[:400]


async def _deploy(client: httpx.AsyncClient, headers: dict, name: str, model: str) -> dict:
    res = await client.post("/deployments", headers=headers, json={
        "name": name, "model": model, "environment": "dev"})
    assert res.status_code == 201, res.text
    return res.json()


async def _mk_echo_handler(client: httpx.AsyncClient, headers: dict, tag: str) -> dict:
    handler_code = (
        "env = input_data.get('payload', {})\n"
        "p = env.get('participant') or {}\n"
        "result = {'text': 'Hi ' + str(p.get('name', '')) + ', got: ' + str(env.get('text', ''))}\n"
    )
    res = await client.post("/workflows", headers=headers, json={"name": f"v69-handler-{tag}", "graph": {
        "nodes": [_node("t", "manual_trigger"), _node("reply", "code", {"code": handler_code})],
        "edges": [_edge("e1", "t", "reply")],
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


# ---------------------------------------------------------------------------
# 1) the provider adapters themselves (pure)
# ---------------------------------------------------------------------------
def test_v69_provider_adapters():
    # ---- telegram: a text update normalizes; edits and stickers skip ----
    update = {"update_id": 9001, "message": {
        "message_id": 7, "from": {"id": 42, "first_name": "Ada", "last_name": "Lovelace"},
        "chat": {"id": 42, "type": "private"}, "text": "I want to order a laptop"}}
    parsed = adapters.telegram_parse_update(update)
    assert parsed.count == 1 and not parsed.skipped
    m = parsed.messages[0]
    assert (m.channel, m.sender_id, m.sender_name, m.text) == (
        "telegram", "42", "Ada Lovelace", "I want to order a laptop")
    assert m.extra["chat_id"] == "42"

    edited = adapters.telegram_parse_update({"update_id": 9002, "edited_message": {"text": "edit"}})
    assert edited.count == 0 and edited.skipped[0]["reason"] == "edited_message"
    sticker = adapters.telegram_parse_update({"update_id": 9003, "message": {
        "from": {"id": 42}, "chat": {"id": 42}, "sticker": {"emoji": "x"}}})
    assert sticker.count == 0 and sticker.skipped[0]["reason"] == "non_text_message"

    assert adapters.telegram_verify_secret("tg-secret", "tg-secret")
    assert not adapters.telegram_verify_secret("other", "tg-secret")
    out = adapters.telegram_build_outbound({"bot_token": "123:ABC"}, "42", "hello")
    assert out["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert out["json"] == {"chat_id": "42", "text": "hello", "parse_mode": "HTML"}

    # ---- meta: multi-message webhook, statuses and captions -------------
    meta_webhook = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "PN1"},
            "contacts": [{"wa_id": "234801", "profile": {"name": "Ada"}}],
            "messages": [
                {"from": "234801", "id": "wamid.1", "type": "text", "text": {"body": "first"}},
                {"from": "234801", "id": "wamid.2", "type": "image", "image": {"caption": "a cat"}},
                {"from": "234801", "id": "wamid.3", "type": "sticker"},
            ]}}]},
            {"id": "WABA1", "changes": [{"field": "messages", "value": {
            "statuses": [{"id": "wamid.0", "status": "delivered"}]}}]}],
    }
    parsed = adapters.meta_parse_webhook(meta_webhook)
    assert parsed.count == 2
    assert parsed.messages[0].text == "first" and parsed.messages[0].sender_name == "Ada"
    assert parsed.messages[1].text == "a cat"  # the caption rides through
    reasons = [s["reason"] for s in parsed.skipped]
    assert "non_text_message" in reasons and "status_update" in reasons

    assert adapters.meta_parse_webhook({"object": "something_else"}).skipped[0]["reason"] == "unsupported_payload"

    # signature: sha256 HMAC over the RAW body against the app secret
    body = b'{"object": "whatsapp_business_account"}'
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    assert adapters.meta_verify_signature("app-secret", body, sig)
    assert not adapters.meta_verify_signature("wrong", body, sig)
    assert not adapters.meta_verify_signature("app-secret", body, "sha256=" + "0" * 64)

    ok, challenge = adapters.meta_verify_handshake(
        {"hub.mode": "subscribe", "hub.verify_token": "vt-1", "hub.challenge": "1158201444"}, "vt-1")
    assert ok and challenge == "1158201444"
    ok, _ = adapters.meta_verify_handshake({"hub.mode": "subscribe", "hub.verify_token": "x"}, "vt-1")
    assert not ok

    out = adapters.meta_build_outbound({"phone_number_id": "PN1", "access_token": "EAAG"}, "234801", "hi")
    assert out["url"].startswith("https://graph.facebook.com/") and out["url"].endswith("/PN1/messages")
    assert out["json"]["to"] == "234801" and out["json"]["text"]["body"] == "hi"
    assert out["headers"]["Authorization"] == "Bearer EAAG"

    # ---- discord: Ed25519 signed interactions ---------------------------
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()
    ts, raw = "1700000000", b'{"type": 1}'
    sig_hex = priv.sign(ts.encode() + raw).hex()
    ok, err = adapters.discord_verify_signature(pub_hex, ts, raw, sig_hex)
    assert ok and err is None
    ok, err = adapters.discord_verify_signature(pub_hex, ts, raw + b" ", sig_hex)
    assert not ok and err == "signature verification failed"
    ok, err = adapters.discord_verify_signature("", ts, raw, sig_hex)
    assert not ok and "public_key" in err

    resp, parsed = adapters.discord_parse_interaction({"type": 1})
    assert resp == {"type": 1} and parsed.count == 0
    resp, parsed = adapters.discord_parse_interaction({
        "type": 2, "id": "i1", "channel_id": "c77",
        "data": {"name": "order", "options": [{"name": "item", "value": "laptop"},
                                              {"name": "qty", "value": "2"}]},
        "member": {"user": {"id": "42", "username": "ada", "global_name": "Ada"}}})
    assert resp is None and parsed.count == 1
    assert parsed.messages[0].text == "order laptop 2"
    assert parsed.messages[0].extra["channel_id"] == "c77"
    out = adapters.discord_build_outbound({"webhook_url": "https://discord.com/api/webhooks/x/y"}, "c77", "hi")
    assert out["url"] == "https://discord.com/api/webhooks/x/y" and out["json"]["content"] == "hi"

    # secrets always masked in API config output
    masked = adapters.mask_config({"app_secret": "supersecret", "phone_number_id": "PN1"})
    assert "supersecret" not in json.dumps(masked) and masked["phone_number_id"] == "PN1"


# ---------------------------------------------------------------------------
# 2) channel endpoints: native webhooks in, replies delivered out
# ---------------------------------------------------------------------------
def test_v69_channel_endpoints():
    tag = uuid.uuid4().hex[:8]
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    # a REAL local HTTP receiver for the outbound delivery test
    received: list[dict] = []

    class _Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            received.append(json.loads(self.rfile.read(length) or b"{}"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *a):  # silence the test runner output
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    hook_url = f"http://127.0.0.1:{server.server_address[1]}/hook"

    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes_raw().hex()

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"chan-{tag}", 1)
            h = _auth(user["token"])
            handler = await _mk_echo_handler(client, h, tag)

            res = await client.get("/channels/adapters", headers=h)
            assert res.status_code == 200
            ids = {a["id"] for a in res.json()["adapters"]}
            assert {"meta_cloud_api", "telegram_bot_api", "discord_bot"} <= ids

            # ---- register the three provider connections ------------------
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": f"tg-{tag}", "provider": "telegram_bot_api",
                "handler_workflow_id": handler["id"],
                "config": {"secret_token": f"tgsec-{tag}", "bot_token": ""}})
            assert res.status_code == 201, res.text
            tg = res.json()
            assert tg["channel"] == "telegram" and tg["webhook_url"].startswith("/api/v1/channels/telegram/")
            tg_path = tg["webhook_url"].replace("/api/v1", "", 1)  # client base_url carries the prefix

            res = await client.post("/channels/endpoints", headers=h, json={
                "name": f"wa-{tag}", "provider": "meta_cloud_api",
                "handler_workflow_id": handler["id"],
                "config": {"verify_token": f"vt-{tag}", "app_secret": f"appsec-{tag}",
                           "phone_number_id": "PN1", "access_token": ""}})
            assert res.status_code == 201, res.text
            wa = res.json()
            wa_path = wa["webhook_url"].replace("/api/v1", "", 1)

            res = await client.post("/channels/endpoints", headers=h, json={
                "name": f"dc-{tag}", "provider": "discord_bot",
                "handler_workflow_id": handler["id"],
                "config": {"public_key": pub_hex, "webhook_url": hook_url}})
            assert res.status_code == 201, res.text
            dc = res.json()
            dc_path = dc["webhook_url"].replace("/api/v1", "", 1)

            # missing provider secrets refuse loudly
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "bad", "provider": "meta_cloud_api", "config": {"verify_token": "x"}})
            assert res.status_code == 400 and "app_secret" in res.json()["detail"]
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "bad", "provider": "carrier_pigeon", "config": {}})
            assert res.status_code == 400 and "unknown provider" in res.json()["detail"]

            # ---- secrets are masked in every API response -----------------
            for ep in (tg, wa, dc):
                blob = json.dumps(ep)
                assert f"tgsec-{tag}" not in blob and f"appsec-{tag}" not in blob and pub_hex not in blob
            listing = (await client.get("/channels/endpoints", headers=h)).json()["endpoints"]
            assert len(listing) == 3 and f"appsec-{tag}" not in json.dumps(listing)

            # ---- TELEGRAM receiver: wrong secret 401, right one answers ---
            update = {"update_id": 1, "message": {
                "message_id": 1, "from": {"id": 42, "first_name": "Ada"},
                "chat": {"id": 42, "type": "private"}, "text": "order a laptop"}}
            res = await client.post(tg_path, json=update,
                                    headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
            assert res.status_code == 401

            res = await client.post(tg_path, json=update,
                                    headers={"X-Telegram-Bot-Api-Secret-Token": f"tgsec-{tag}"})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["ok"] is True and body["received"] == 1
            handled = body["handled"][0]
            assert handled["reply"] == "Hi Ada, got: order a laptop"
            assert handled["delivery"] == "skipped" and "bot_token" in handled["detail"]
            conv_id = handled["conversation_id"]

            # the transcript lives in the interaction layer, provider-stamped
            res = await client.get(f"/interactions/conversations/{conv_id}", headers=h)
            msgs = res.json()["messages"]
            assert [m["role"] for m in msgs if m["role"] in ("user", "agent")] == ["user", "agent"]
            user_msg = next(m for m in msgs if m["role"] == "user")
            assert user_msg["payload"]["provider"] == "telegram_bot_api"

            # an edit update is honestly skipped, not answered
            res = await client.post(tg_path,
                                    json={"update_id": 2, "edited_message": {"text": "x"}},
                                    headers={"X-Telegram-Bot-Api-Secret-Token": f"tgsec-{tag}"})
            assert res.status_code == 200 and res.json()["received"] == 0
            assert res.json()["skipped"][0]["reason"] == "edited_message"

            # ---- WHATSAPP receiver: handshake + HMAC-signed messages ------
            res = await client.get(wa_path, params={
                "hub.mode": "subscribe", "hub.verify_token": f"vt-{tag}", "hub.challenge": "1158201444"})
            assert res.status_code == 200 and res.text == "1158201444"
            res = await client.get(wa_path, params={
                "hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "x"})
            assert res.status_code == 403

            wa_payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": "PN1"},
                "contacts": [{"wa_id": "234801", "profile": {"name": "Grace"}}],
                "messages": [{"from": "234801", "id": "wamid.9", "type": "text",
                              "text": {"body": "refill the order"}}]}}]}]}
            raw = json.dumps(wa_payload).encode()
            bad_sig = {"X-Hub-Signature-256": "sha256=" + "0" * 64, "Content-Type": "application/json"}
            res = await client.post(wa_path, content=raw, headers=bad_sig)
            assert res.status_code == 403

            sig = "sha256=" + hmac.new(f"appsec-{tag}".encode(), raw, hashlib.sha256).hexdigest()
            res = await client.post(wa_path, content=raw,
                                    headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
            assert res.status_code == 200, res.text
            handled = res.json()["handled"][0]
            assert handled["reply"] == "Hi Grace, got: refill the order"

            # ---- DISCORD receiver: PING handshake + signed command --------
            ping_raw = json.dumps({"type": 1}).encode()
            ts = "1700000000"
            res = await client.post(dc_path, content=ping_raw,
                                    headers={"X-Signature-Ed25519": "00", "X-Signature-Timestamp": ts})
            assert res.status_code == 401
            sig_hex = priv.sign(ts.encode() + ping_raw).hex()
            res = await client.post(dc_path, content=ping_raw,
                                    headers={"X-Signature-Ed25519": sig_hex, "X-Signature-Timestamp": ts})
            assert res.status_code == 200 and res.json() == {"type": 1}

            cmd = {"type": 2, "id": "i1", "channel_id": "c77",
                   "data": {"name": "order", "options": [{"name": "item", "value": "laptop"}]},
                   "member": {"user": {"id": "42", "username": "ada", "global_name": "Ada"}}}
            cmd_raw = json.dumps(cmd).encode()
            sig_hex = priv.sign(ts.encode() + cmd_raw).hex()
            res = await client.post(dc_path, content=cmd_raw,
                                    headers={"X-Signature-Ed25519": sig_hex, "X-Signature-Timestamp": ts})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["type"] == 4 and "Hi Ada, got: order laptop" in body["data"]["content"]
            # the reply was DELIVERED to the local receiver through the
            # provider's execute-webhook request
            await asyncio.sleep(0.2)
            assert received and "got: order laptop" in json.dumps(received[-1])

            # ---- preview-outbound: the exact request, secrets masked ------
            res = await client.post(f"/channels/endpoints/{tg['id']}/preview-outbound",
                                    headers=h, json={"to": "42", "text": "hello there"})
            assert res.status_code == 200, res.text
            prev = res.json()
            assert prev["url"].endswith("/sendMessage")
            assert prev["would_deliver"] is False and "bot_token" in prev["missing_credentials"]

            # ---- endpoint governance: disable, delete ---------------------
            res = await client.put(f"/channels/endpoints/{wa['id']}", headers=h,
                                   json={"enabled": False})
            assert res.status_code == 200 and res.json()["enabled"] is False
            raw2 = json.dumps(wa_payload).encode()
            sig2 = "sha256=" + hmac.new(f"appsec-{tag}".encode(), raw2, hashlib.sha256).hexdigest()
            res = await client.post(wa_path, content=raw2,
                                    headers={"X-Hub-Signature-256": sig2})
            assert res.status_code == 403 and "disabled" in res.json()["detail"]

            res = await client.delete(f"/channels/endpoints/{dc['id']}", headers=h)
            assert res.status_code == 200
            res = await client.post(dc_path, content=ping_raw, headers={
                "X-Signature-Ed25519": sig_hex, "X-Signature-Timestamp": ts})
            assert res.status_code == 404

            # ownership: another user's endpoints look nonexistent
            other = await _mk_user(client, f"chan-{tag}", 2)
            res = await client.get(f"/channels/endpoints/{tg['id']}", headers=_auth(other["token"]))
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        server.shutdown()
        serving_limits.reset_all()


# ---------------------------------------------------------------------------
# 3) voice session primitives
# ---------------------------------------------------------------------------
def test_v69_voice_primitives():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"vox-{tag}", 1)
            h = _auth(user["token"])
            handler = await _mk_echo_handler(client, h, tag)

            res = await client.get("/voice/contracts", headers=h)
            assert res.status_code == 200
            contracts = res.json()
            assert "transcript" in contracts["asr"]["contract"]["output"]
            assert "openai_whisper" in contracts["asr"]["providers"]
            assert "elevenlabs" in contracts["tts"]["providers"]
            assert contracts["call_states"] == ["initiated", "ringing", "in_progress",
                                                "on_hold", "voicemail", "ended"]

            # ---- open a call: it links (opens) an interaction conversation -
            res = await client.post("/voice/sessions", headers=h, json={
                "direction": "inbound", "provider": "twilio", "call_ref": f"CA{tag}",
                "from_ref": "+234-801", "to_ref": "+234-900", "handler_workflow_id": handler["id"]})
            assert res.status_code == 201, res.text
            sess = res.json()
            assert sess["state"] == "initiated" and sess["conversation_id"]

            # ---- the state machine: ringing -> answered --------------------
            res = await client.post(f"/voice/sessions/{sess['id']}/events", headers=h,
                                    json={"kind": "call.ringing"})
            assert res.json()["state"] == "ringing"
            # an illegal transition is refused, not absorbed
            res = await client.post(f"/voice/sessions/{sess['id']}/events", headers=h,
                                    json={"kind": "no_answer"})
            assert res.status_code == 200  # ringing -> ended(no_answer) is legal...

            # ...so open a fresh session to test the truly illegal hop
            res = await client.post("/voice/sessions", headers=h, json={
                "direction": "inbound", "from_ref": "+234-802", "handler_workflow_id": handler["id"]})
            sess2 = res.json()
            res = await client.post(f"/voice/sessions/{sess2['id']}/events", headers=h,
                                    json={"kind": "call.answered"})
            assert res.status_code == 200  # initiated -> in_progress is legal (caller skips ringing)
            res = await client.post(f"/voice/sessions/{sess2['id']}/events", headers=h,
                                    json={"kind": "unhold"})
            assert res.status_code == 400 and "unhold" in res.json()["detail"]

            # ---- session 1: answer and run a voice TURN -------------------
            res = await client.post(f"/voice/sessions/{sess['id']}/events", headers=h,
                                    json={"kind": "call.ringing"})
            assert res.status_code == 400  # already ended (no_answer above)

            res = await client.post("/voice/sessions", headers=h, json={
                "direction": "inbound", "provider": "twilio", "from_ref": "+234-803",
                "handler_workflow_id": handler["id"]})
            sess3 = res.json()
            await client.post(f"/voice/sessions/{sess3['id']}/events", headers=h,
                              json={"kind": "call.answered"})

            res = await client.post(f"/voice/sessions/{sess3['id']}/turn", headers=h,
                                    json={"transcript": "I want to order a laptop", "confidence": 0.93})
            assert res.status_code == 200, res.text
            turn = res.json()
            assert turn["asr"]["transcript"] == "I want to order a laptop"
            assert turn["reply"] == "Hi , got: I want to order a laptop"
            # the TTS contract is bound and interruptible
            assert turn["tts"]["provider"] == "openai_tts" and turn["tts"]["barge_in_ok"] is True
            assert turn["tts"]["text"] == turn["reply"] and turn["tts"]["tts_id"]
            assert turn["state"] == "in_progress"

            # the turn lives on the LINKED conversation with voice stamps
            res = await client.get(f"/interactions/conversations/{sess3['conversation_id']}", headers=h)
            msgs = [m for m in res.json()["messages"] if m["role"] in ("user", "agent")]
            assert [(m["role"], m["channel"]) for m in msgs] == [("user", "voice"), ("agent", "voice")]
            assert msgs[0]["payload"]["confidence"] == 0.93

            # ---- BARGE-IN: the caller interrupts the utterance ------------
            res = await client.post(f"/voice/sessions/{sess3['id']}/barge-in", headers=h)
            assert res.status_code == 200, res.text
            barge = res.json()
            assert barge["interrupted"] == turn["tts"]["tts_id"] and barge["barge_in_count"] == 1

            res = await client.post(f"/voice/sessions/{sess3['id']}/barge-in", headers=h)
            assert res.status_code == 400 and "nothing is playing" in res.json()["detail"]

            # a fresh turn opens a new utterance; completing it closes it
            res = await client.post(f"/voice/sessions/{sess3['id']}/turn", headers=h,
                                    json={"transcript": "two laptops please", "confidence": 0.97})
            assert res.json()["tts"]["tts_id"] != turn["tts"]["tts_id"]
            res = await client.post(f"/voice/sessions/{sess3['id']}/tts/complete", headers=h)
            assert res.status_code == 200

            # session stats are derived from the event timeline
            res = await client.get(f"/voice/sessions/{sess3['id']}", headers=h)
            full = res.json()
            assert full["turn_count"] == 2 and full["barge_in_count"] == 1
            assert full["active_tts"] is False and full["state"] == "in_progress"
            assert any(e["kind"] == "barge_in" and e["payload"]["interrupted"] for e in full["events"])

            # a bad ASR confidence refuses at the boundary
            res = await client.post(f"/voice/sessions/{sess3['id']}/turn", headers=h,
                                    json={"transcript": "x", "confidence": 1.5})
            assert res.status_code == 422  # pydantic ge/le

            # ---- hold / transfer / hangup ---------------------------------
            res = await client.post(f"/voice/sessions/{sess3['id']}/events", headers=h,
                                    json={"kind": "hold"})
            assert res.json()["state"] == "on_hold"
            res = await client.post(f"/voice/sessions/{sess3['id']}/turn", headers=h,
                                    json={"transcript": "hello?"})
            assert res.status_code == 400 and "in_progress" in res.json()["detail"]
            res = await client.post(f"/voice/sessions/{sess3['id']}/events", headers=h,
                                    json={"kind": "unhold"})
            res = await client.post(f"/voice/sessions/{sess3['id']}/events", headers=h,
                                    json={"kind": "transfer", "payload": {"target": "+234-999"}})
            assert res.status_code == 200
            res = await client.post(f"/voice/sessions/{sess3['id']}/events", headers=h,
                                    json={"kind": "hangup"})
            assert res.json()["state"] == "ended" and res.json()["end_reason"] == "hangup"
            res = await client.post(f"/voice/sessions/{sess3['id']}/turn", headers=h,
                                    json={"transcript": "still there?"})
            assert res.status_code == 400 and "already ended" in res.json()["detail"]

            # ---- TWILIO call-status callback (the voice provider adapter) -
            res = await client.post("/voice/sessions", headers=h, json={
                "direction": "outbound", "provider": "twilio", "to_ref": "+234-801",
                "handler_workflow_id": handler["id"]})
            dial = res.json()
            res = await client.post(f"/voice/webhooks/twilio/{dial['id']}", headers=h,
                                    json={"CallSid": f"CA-dial-{tag}", "CallStatus": "ringing"})
            assert res.json()["state"] == "ringing"
            res = await client.post(f"/voice/webhooks/twilio/{dial['id']}", headers=h,
                                    json={"CallSid": f"CA-dial-{tag}", "CallStatus": "in-progress"})
            assert res.json()["state"] == "in_progress"
            res = await client.get(f"/voice/sessions/{dial['id']}", headers=h)
            assert res.json()["call_ref"] == f"CA-dial-{tag}"
            res = await client.post(f"/voice/webhooks/twilio/{dial['id']}", headers=h,
                                    json={"CallSid": f"CA-dial-{tag}", "CallStatus": "no-answer"})
            assert res.json()["state"] == "ended" and res.json()["end_reason"] == "no_answer"
            res = await client.post(f"/voice/webhooks/twilio/{dial['id']}", headers=h,
                                    json={"CallSid": f"CA-dial-{tag}", "CallStatus": "completed"})
            assert res.status_code == 400 and "already ended" in res.json()["detail"]

            # unknown statuses refuse honestly
            res = await client.post("/voice/sessions", headers=h, json={"direction": "outbound"})
            s4 = res.json()
            res = await client.post(f"/voice/webhooks/twilio/{s4['id']}", headers=h,
                                    json={"CallSid": "CA-x", "CallStatus": "carrier-pigeon"})
            assert res.status_code == 400 and "CallStatus" in res.json()["detail"]

            # conversation_ref continuity: a second call joins the SAME transcript
            res = await client.post("/voice/sessions", headers=h, json={
                "direction": "inbound", "from_ref": "+234-803",
                "conversation_ref": sess3["conversation_id"]})
            assert res.json()["conversation_id"] == sess3["conversation_id"]

            # ownership scoping
            other = await _mk_user(client, f"vox-{tag}", 2)
            res = await client.get(f"/voice/sessions/{sess3['id']}", headers=_auth(other["token"]))
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        serving_limits.reset_all()


# ---------------------------------------------------------------------------
# 4) rate-shaping / quotas on serving tokens
# ---------------------------------------------------------------------------
def test_v69_serving_limits():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"lim-{tag}", 1)
            h = _auth(user["token"])
            await _train_lm(client, tag, h, f"lim-model-{tag}")

            # a second serving path (open) to keep the token scenarios isolated
            dep = await _deploy(client, h, f"lim endpoint {tag}", f"lim-model-{tag}")
            wf_id = dep["workflow"]["id"]

            # ---- rate shaping: 2/min, the 3rd is shaped --------------------
            res = await client.post(f"/deployments/{dep['id']}/tokens", headers=h,
                                    json={"name": "shaped", "rate_per_min": 2})
            assert res.status_code == 201, res.text
            shaped = res.json()
            assert shaped["limits"] == {"rate_per_min": 2, "daily_quota": None}
            assert shaped["usage"]["rate_per_min"] == 2

            call = {"Authorization": f"Bearer {shaped['token']}"}
            for i in range(2):
                res = await client.post(f"/webhooks/{wf_id}", json={"prompt": f"the agent {i}"}, headers=call)
                assert res.status_code == 200, res.text
            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"}, headers=call)
            assert res.status_code == 429, res.text
            assert "rate limit exceeded" in res.json()["detail"]
            assert int(res.headers["Retry-After"]) >= 1
            assert res.headers["X-RateLimit-Limit"] == "2"
            assert res.headers["X-RateLimit-Remaining"] == "0"

            # the shaped request never reached the model (only 2 executions)
            res = await client.get(f"/deployments/{dep['id']}", headers=h)
            assert res.json()["stats"]["runs_7d"] == 2

            # ---- daily quota: 2/day, the 3rd exhausts ----------------------
            res = await client.post(f"/deployments/{dep['id']}/tokens", headers=h,
                                    json={"name": "quotal", "daily_quota": 2})
            quotal = res.json()
            qcall = {"X-Deployment-Token": quotal["token"]}
            for i in range(2):
                res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"}, headers=qcall)
                assert res.status_code == 200, res.text
            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"}, headers=qcall)
            assert res.status_code == 429 and "daily quota exhausted" in res.json()["detail"]
            assert res.headers["X-Quota-Limit"] == "2" and res.headers["X-Quota-Used"] == "2"
            assert "T00:00:00" in res.headers["X-Quota-Reset"]  # next UTC midnight

            # ---- an unlimited token keeps working (shaping is opt-in) ------
            res = await client.post(f"/deployments/{dep['id']}/tokens", headers=h,
                                    json={"name": "free"})
            free = res.json()
            fcall = {"Authorization": f"Bearer {free['token']}"}
            for i in range(3):
                res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"}, headers=fcall)
                assert res.status_code == 200, res.text

            # ---- usage endpoint: live counters per token -------------------
            res = await client.get(f"/deployments/{dep['id']}/tokens/{quotal['id']}/usage", headers=h)
            usage = res.json()
            assert usage["usage"]["day_used"] == 2 and usage["usage"]["daily_quota"] == 2
            assert usage["usage"]["quota_day"]  # today's UTC date
            assert usage["token"]["limits"]["daily_quota"] == 2

            # ---- PUT limits: shape the free token, clear it again ----------
            res = await client.put(f"/deployments/{dep['id']}/tokens/{free['id']}/limits",
                                   headers=h, json={"rate_per_min": 1})
            assert res.status_code == 200, res.text
            assert res.json()["limits"]["rate_per_min"] == 1
            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"}, headers=fcall)
            assert res.status_code == 429  # the free token is shaped NOW
            res = await client.put(f"/deployments/{dep['id']}/tokens/{free['id']}/limits",
                                   headers=h, json={"rate_per_min": None, "daily_quota": None})
            assert res.json()["limits"] == {"rate_per_min": None, "daily_quota": None}

            res = await client.put(f"/deployments/{dep['id']}/tokens/ghost/limits",
                                   headers=h, json={"rate_per_min": 5})
            assert res.status_code == 404

            # ---- the SSE stream enforces the same limits -------------------
            res = await client.post(f"/deployments/{dep['id']}/tokens", headers=h,
                                    json={"name": "streamer", "rate_per_min": 1})
            streamer = res.json()
            scall = {"Authorization": f"Bearer {streamer['token']}"}
            res = await client.post(f"/deployments/{dep['id']}/stream", headers=scall,
                                    json={"prompt": "the agent", "max_tokens": 4})
            assert res.status_code == 200, res.text
            events = _parse_sse(res.text)
            assert events[-1][0] == "done"
            res = await client.post(f"/deployments/{dep['id']}/stream", headers=scall,
                                    json={"prompt": "the agent", "max_tokens": 4})
            assert res.status_code == 429 and "rate limit exceeded" in res.json()["detail"]

            # a shaped request leaves the open path untouched
            res = await client.post(f"/webhooks/{wf_id}", json={"prompt": "the agent"})
            assert res.status_code == 401  # still token-gated, unaffected by shaping

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        serving_limits.reset_all()
