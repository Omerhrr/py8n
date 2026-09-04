"""V71 feature tests: the email + SMS channels (completing the provider
matrix) and the Voice Agent builder experience.

- email_inbound: the long-form channel rides TWO webhook-native shapes -
  the signed-JSON mail contract (X-Py8n-Signature HMAC-SHA256, the same
  scheme as the any-gateway adapters) and raw-MIME multipart (the
  SendGrid Inbound Parse shape, parsed with the stdlib email parser:
  headers, text/plain body, attachments counted then honestly skipped).
  Outbound is SMTP: email_build_outbound returns the RFC 5322 message +
  envelope, delivery is honest (skipped without credentials, the reply
  still recorded in the transcript), and replies thread "Re: <subject>".
- telnyx_sms + generic_sms: Telnyx Messaging rides the SAME RFC 9421
  signatures as voice (message.received in, delivery statuses skipped);
  generic_sms is the ANY-gateway contract - {from, to, text} JSON +
  HMAC - so Twilio relays, Vonage, Africa's Talking or a GSM modem box
  all carry SMS through py8n without a vendor named in code.
- voice agents: the builder object over the v69/v70 voice primitives -
  create (with a SCAFFOLDED, runnable handler workflow), inherit the
  speech config into sessions (greeting TTS on answered, ASR engine
  selection on the media stream, TTS voice on turns), barge-in over the
  greeting, ownership refusals - and the media stream reports the agent
  binding on connect with the stream's customParameters still winning.

Runs the FastAPI app in-process (httpx ASGITransport); the websocket
test reuses the in-loop ASGI client discipline from v70.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import struct
import uuid
from email.message import EmailMessage

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.services import channel_adapters as adapters
from app.services import executor as executor_mod
from app.services import voice_transport as transport

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v71-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v71 u{n} {tag}",
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


async def _mk_echo_handler(client: httpx.AsyncClient, headers: dict, tag: str) -> dict:
    handler_code = (
        "env = input_data.get('payload', {})\n"
        "meta = env.get('metadata') or {}\n"
        "result = {'text': 'Hi ' + str(env.get('participant', {}).get('name', ''))\n"
        "         + ', got: ' + str(env.get('text', ''))\n"
        "         + (' [persona: ' + str(meta.get('system_prompt', '')) + ']' if meta.get('system_prompt') else '')}\n"
    )
    res = await client.post("/workflows", headers=headers, json={"name": f"v71-handler-{tag}", "graph": {
        "nodes": [_node("t", "manual_trigger"), _node("reply", "code", {"code": handler_code})],
        "edges": [_edge("e1", "t", "reply")],
    }})
    assert res.status_code in (200, 201), res.text
    return res.json()


def _sign(secret: str, raw: bytes) -> str:
    return adapters.generic_sms_sign(secret, raw)


def _wh(in_process_url: str) -> str:
    """The v69 gotcha: webhook_url in API output carries the /api/v1 prefix;
    the in-process client's base_url already includes it."""
    assert in_process_url.startswith("/api/v1/")
    return in_process_url[len("/api/v1"):]


# ---------------------------------------------------------------------------
# the in-loop ASGI websocket client (the v70 discipline, one event loop)
# ---------------------------------------------------------------------------

class _WSClient:
    def __init__(self, app, path: str, token: str | None = None):
        self._app = app
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

    async def receive_text(self) -> str:
        while True:
            try:
                msg = await asyncio.wait_for(self._outgoing.get(), 45)
            except asyncio.TimeoutError:
                if self._task is not None and self._task.done():
                    self._task.result()
                raise AssertionError("no websocket frame within 45s")
            if msg["type"] == "websocket.send":
                return msg.get("text") or ""
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


def _pcm(samples: list[int]) -> str:
    return base64.b64encode(struct.pack(f"<{len(samples)}h", *samples)).decode()


SILENT_200MS = _pcm([0] * 1600)
LOUD_200MS = _pcm([9000] * 1600)


def _media(payload: str, chunk: int, encoding: str = "linear16", extra: dict | None = None) -> str:
    media = {"payload": payload, "track": "inbound", "chunk": chunk,
             "encoding": encoding, "sample_rate": 8000}
    if extra:
        media.update(extra)
    return json.dumps({"event": "media", "media": media})


# ---------------------------------------------------------------------------
# RFC 9421 test keypair (the telnyx_sms receiver signs exactly like voice)
# ---------------------------------------------------------------------------

def _ed25519_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub_pem


def _rfc9421_headers(priv, raw: bytes, target: str, *, method: str = "POST",
                     components: tuple[str, ...] = ("@method", "@target", "content-digest")) -> dict:
    covered = " ".join(f'"{c}"' for c in components)
    sig_input = f'sig1=({covered});created=1618884473;keyid="k1"'
    lines = []
    for comp in components:
        if comp == "@method":
            lines.append(f'"@method": {method}')
        elif comp == "@target":
            lines.append(f'"@target": {target}')
        elif comp == "content-digest":
            digest = base64.b64encode(hashlib.sha256(raw).digest()).decode()
            lines.append(f'"content-digest": sha-256=:{digest}:')
    lines.append(f'"@signature-params": ({covered});created=1618884473;keyid="k1"')
    base = "\n".join(lines)
    sig = base64.b64encode(priv.sign(base.encode("utf-8"))).decode()
    headers = {"signature-input": sig_input, "signature": f"sig1=:{sig}:"}
    if "content-digest" in components:
        digest = base64.b64encode(hashlib.sha256(raw).digest()).decode()
        headers["content-digest"] = f"sha-256=:{digest}:"
    return headers


# ---------------------------------------------------------------------------
# 1) the email channel: signed JSON + raw MIME in, SMTP out
# ---------------------------------------------------------------------------
def test_v71_email_channel():
    tag = uuid.uuid4().hex[:8]

    # ---- pure units: the HMAC contract ---------------------------------
    raw = b'{"from": "a@x.test", "text": "hello"}'
    good = _sign("sekrit", raw)
    assert adapters.hmac_verify("sekrit", raw, good)
    assert adapters.hmac_verify("sekrit", raw, "sha256=" + good.removeprefix("sha256="))
    assert not adapters.hmac_verify("sekrit", b"tampered", good)
    assert not adapters.hmac_verify("wrong", raw, good)
    assert not adapters.hmac_verify("", raw, good)          # no secret -> fail closed
    assert not adapters.hmac_verify("sekrit", raw, "")      # no header -> fail closed

    # ---- pure units: the JSON mail shape --------------------------------
    result = adapters.email_parse_webhook({
        "from": {"address": "ada@lovelace.test", "name": "Ada Lovelace"},
        "to": "support@py8n.test", "subject": "Engine trouble",
        "text": "My analytical engine stopped.", "message_id": "<m1@x>",
        "in_reply_to": "", "attachments": [{"filename": "log.txt", "size": 3}],
    })
    assert result.count == 1
    msg = result.messages[0]
    assert (msg.channel, msg.sender_id, msg.sender_name) == ("email", "ada@lovelace.test", "Ada Lovelace")
    assert msg.text == "My analytical engine stopped."
    assert msg.extra["subject"] == "Engine trouble" and msg.extra["attachment_count"] == 1
    assert result.skipped and result.skipped[0]["reason"] == "attachments_noted"

    # html-only mail is honest about not transcribing
    result = adapters.email_parse_webhook({"from": "b@x.test", "html": "<p>rich</p>"})
    assert result.count == 0 and result.skipped[0]["reason"] == "non_text_message"
    # no sender -> honest skip
    result = adapters.email_parse_webhook({"text": "anon"})
    assert result.count == 0 and result.skipped[0]["reason"] == "no_sender"

    # ---- pure units: raw MIME parsing (stdlib parser) -------------------
    mime = EmailMessage()
    mime["From"] = "Grace Hopper <grace@navy.test>"
    mime["To"] = "support@py8n.test"
    mime["Subject"] = "Compiler bug"
    mime["Message-ID"] = "<bug1@navy.test>"
    mime.set_content("The compiler throws a null reference.")
    mime.add_attachment(b"PK\x03\x04", maintype="application", subtype="zip", filename="core.zip")
    parsed = adapters.email_parse_mime(mime.as_string())
    assert parsed["from"] == "grace@navy.test" and parsed["from_name"] == "Grace Hopper"
    assert parsed["subject"] == "Compiler bug" and parsed["attachment_count"] == 1
    assert "null reference" in parsed["text"] and parsed["message_id"] == "<bug1@navy.test>"
    # the parsed MIME feeds the SAME JSON parse path
    result = adapters.email_parse_webhook({**parsed, "via_mime": True})
    assert result.count == 1 and result.messages[0].sender_id == "grace@navy.test"

    # ---- pure units: the SMTP outbound shape ----------------------------
    request = adapters.email_build_outbound(
        {"from_address": "robot@py8n.test", "smtp_host": "mx.py8n.test"}, "ada@lovelace.test",
        "We got your message.", subject="Re: Engine trouble")
    assert request["transport"] == "smtp" and request["host"] == "mx.py8n.test"
    assert request["from"] == "robot@py8n.test" and request["to"] == "ada@lovelace.test"
    assert request["subject"] == "Re: Engine trouble"
    assert "We got your message." in request["message"]
    assert "From: " in request["message"] and "robot@py8n.test" in request["message"]
    assert "X-Py8n-Channel: email" in request["message"]
    # a missing from_address builds honestly; the delivery layer reports it
    request = adapters.email_build_outbound({}, "x@y.test", "hi")
    assert request["from"] == "" and request["transport"] == "smtp"
    with pytest.raises(ValueError, match="smtp_host"):
        adapters.email_send_smtp({"host": "", "from": "a@b", "to": "c@d", "message": "m"})

    # ---- E2E: signed JSON webhook -> transcript + honest SMTP skip ------
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"email-{tag}", 1)
            h = _auth(user["token"])
            handler = await _mk_echo_handler(client, h, f"email-{tag}")
            secret = "mail-sekrit-" + tag
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": f"support inbox {tag}", "provider": "email_inbound",
                "handler_workflow_id": handler["id"], "config": {"secret": secret}})
            assert res.status_code == 201, res.text
            ep = res.json()
            assert ep["channel"] == "email" and ep["config"]["secret"].endswith(f"({len(secret)} chars)")

            body = json.dumps({
                "from": {"address": "ada@lovelace.test", "name": "Ada Lovelace"},
                "to": "support@py8n.test", "subject": "Engine trouble",
                "text": "My analytical engine stopped.", "message_id": f"<m1-{tag}@x>"})
            res = await client.post(_wh(ep["webhook_url"]), content=body.encode(),
                                    headers={"content-type": "application/json",
                                             "x-py8n-signature": _sign(secret, body.encode())})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["received"] == 1 and not out["skipped"]
            delivery = out["handled"][0]
            assert delivery["delivery"] == "skipped"
            assert "smtp_host" in delivery["detail"] and "from_address" in delivery["detail"]
            assert delivery["request"]["transport"] == "smtp"
            assert delivery["request"]["subject"] == "Re: Engine trouble"
            assert delivery["request"]["to"] == "ada@lovelace.test"

            # a SECOND email from the same sender threads into the SAME
            # conversation (the sender-keyed find-or-create)
            body2 = json.dumps({
                "from": {"address": "ada@lovelace.test"}, "subject": "Re: Engine trouble",
                "text": "Any update?", "message_id": f"<m2-{tag}@x>",
                "in_reply_to": f"<m1-{tag}@x>"})
            res = await client.post(_wh(ep["webhook_url"]), content=body2.encode(),
                                    headers={"content-type": "application/json",
                                             "x-py8n-signature": _sign(secret, body2.encode())})
            conv_id = out["handled"][0]["conversation_id"]
            assert res.json()["handled"][0]["conversation_id"] == conv_id
            res = await client.get(f"/interactions/conversations/{conv_id}", headers=h)
            conv = res.json()
            assert conv["channel"] == "email" and conv["participant"]["id"] == "ada@lovelace.test"
            texts = [(m["role"], m["text"]) for m in conv["messages"] if m["role"] in ("user", "agent")]
            assert texts[0][1] == "My analytical engine stopped."
            assert texts[2][1] == "Any update?"  # user2 - one thread, two emails

            # the raw-MIME multipart shape rides the SAME receiver: HMAC over
            # the RAW urlencoded body, the MIME parsed server-side
            mime = EmailMessage()
            mime["From"] = "grace@navy.test"
            mime["To"] = "support@py8n.test"
            mime["Subject"] = "Compiler bug"
            mime.set_content("The compiler throws a null reference.")
            from urllib.parse import urlencode
            form_body = urlencode({"email": mime.as_string(), "envelope": "{}"}).encode()
            res = await client.post(_wh(ep["webhook_url"]), content=form_body,
                                    headers={"content-type": "application/x-www-form-urlencoded",
                                             "x-py8n-signature": _sign(secret, form_body)})
            assert res.status_code == 200, res.text
            out2 = res.json()
            assert out2["received"] == 1 and not out2["skipped"]
            assert out2["handled"][0]["text"].strip() == "The compiler throws a null reference."
            assert out2["handled"][0]["delivery"] == "skipped"

            # tampered signature -> 403 before anything runs
            res = await client.post(_wh(ep["webhook_url"]), content=b'{"from":"e@x.test","text":"evil"}',
                                    headers={"content-type": "application/json",
                                             "x-py8n-signature": _sign("wrong", b'{"from":"e@x.test","text":"evil"}')})
            assert res.status_code == 403
            # a missing signature header -> 403 (fail closed)
            res = await client.post(_wh(ep["webhook_url"]), content=b'{"from":"e@x.test","text":"evil"}',
                                    headers={"content-type": "application/json"})
            assert res.status_code == 403

            # preview-outbound shows the SMTP request, secrets masked
            res = await client.post(f"/channels/endpoints/{ep['id']}/preview-outbound", headers=h,
                                    json={"to": "ada@lovelace.test", "text": "reply!",
                                          "subject": "Re: Engine trouble"})
            assert res.status_code == 200, res.text
            preview = res.json()
            assert preview["transport"] == "smtp" and preview["would_deliver"] is False
            assert "reply!" in preview["message_preview"]
            assert preview["subject"] == "Re: Engine trouble"

            # ownership: a stranger cannot see the endpoint
            stranger = await _mk_user(client, f"email-{tag}", 2)
            res = await client.get(f"/channels/endpoints/{ep['id']}", headers=_auth(stranger["token"]))
            assert res.status_code == 404

            # events_received counted all three webhooks (including the 403s? no - refused ones never reach the counter)
            res = await client.get(f"/channels/endpoints/{ep['id']}", headers=h)
            assert res.json()["events_received"] == 3

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 2) the SMS channels: telnyx_sms (RFC 9421) + generic_sms (any gateway)
# ---------------------------------------------------------------------------
def test_v71_sms_channels():
    tag = uuid.uuid4().hex[:8]

    # ---- pure units: telnyx_sms parse ------------------------------------
    result = adapters.telnyx_sms_parse_webhook({"data": {"event_type": "message.received", "payload": {
        "id": f"msg-{tag}", "from": {"phone_number": "+234801"},
        "to": [{"phone_number": "+234802"}], "text": "Where is my order?"}}})
    assert result.count == 1
    m = result.messages[0]
    assert (m.channel, m.sender_id, m.text) == ("sms", "+234801", "Where is my order?")
    assert m.extra["to"] == "+234802" and m.event_id == f"msg-{tag}"
    # delivery statuses are honest skips - not messages
    for evt in ("message.finalized", "message.sent", "message.queued"):
        result = adapters.telnyx_sms_parse_webhook({"data": {"event_type": evt, "payload": {}}})
        assert result.count == 0 and result.skipped[0]["reason"] == "status_update", evt
    result = adapters.telnyx_sms_parse_webhook({"data": {"event_type": "call.initiated", "payload": {}}})
    assert result.count == 0 and result.skipped[0]["reason"] == "unhandled_event_type"
    result = adapters.telnyx_sms_parse_webhook({"data": {"event_type": "message.received", "payload": {"text": ""}}})
    assert result.count == 0 and result.skipped[0]["reason"] == "non_text_message"

    # ---- pure units: the outbound builds ---------------------------------
    req = adapters.telnyx_sms_build_outbound({"api_key": "tk_", "from_number": "+234802"}, "+234801", "hi")
    assert req["url"] == "https://api.telnyx.com/v2/messages"
    assert req["json"] == {"to": "+234801", "from": "+234802", "text": "hi"}
    # a missing credential builds honestly (empty sender) - the delivery
    # layer reports it as "skipped: missing from_number"
    req = adapters.telnyx_sms_build_outbound({}, "+234801", "hi")
    assert req["json"]["from"] == ""

    req = adapters.generic_sms_build_outbound(
        {"send_url": "https://gw.test/send", "bearer_token": "bt", "from_number": "PY8N"}, "+1555", "hello")
    assert req["url"] == "https://gw.test/send"
    assert req["json"] == {"to": "+1555", "from": "PY8N", "text": "hello"}
    assert req["headers"]["Authorization"] == "Bearer bt"
    req = adapters.generic_sms_build_outbound({}, "+1555", "hello")
    assert req["url"] == ""  # the delivery layer reports the missing send_url

    # the generic contract parses aliases (body/msisdn) and skips honestly
    result = adapters.generic_sms_parse_webhook({"from": "+1", "body": "via body key"})
    assert result.count == 1 and result.messages[0].text == "via body key"
    result = adapters.generic_sms_parse_webhook({"msisdn": "+2", "text": "hi"})
    assert result.count == 1 and result.messages[0].sender_id == "+2"
    result = adapters.generic_sms_parse_webhook({"from": "+1"})
    assert result.count == 0 and result.skipped[0]["reason"] == "non_text_message"
    result = adapters.generic_sms_parse_webhook({"text": "no sender"})
    assert result.count == 0 and result.skipped[0]["reason"] == "no_sender"

    # ---- E2E: the any-gateway receiver answers a signed SMS --------------
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"sms-{tag}", 1)
            h = _auth(user["token"])
            handler = await _mk_echo_handler(client, h, f"sms-{tag}")
            secret = "sms-sekrit-" + tag
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": f"any-gateway {tag}", "provider": "generic_sms",
                "handler_workflow_id": handler["id"], "config": {"secret": secret}})
            assert res.status_code == 201, res.text
            ep = res.json()
            assert ep["webhook_url"].startswith("/api/v1/channels/sms/")

            body = json.dumps({"from": "+234801", "to": "PY8N", "text": "Where is my order?",
                               "id": f"sms-{tag}"})
            res = await client.post(_wh(ep["webhook_url"]), content=body.encode(),
                                    headers={"content-type": "application/json",
                                             "x-py8n-signature": _sign(secret, body.encode())})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["received"] == 1 and not out["skipped"]
            assert out["handled"][0]["reply"] == "Hi , got: Where is my order?"
            assert out["handled"][0]["delivery"] == "skipped"  # no send_url configured
            assert "send_url" in out["handled"][0]["detail"]

            # tampered -> 401
            res = await client.post(_wh(ep["webhook_url"]), content=body.encode(),
                                    headers={"content-type": "application/json",
                                             "x-py8n-signature": _sign("wrong", body.encode())})
            assert res.status_code == 401

            # ---- telnyx_sms E2E: RFC 9421, the SAME signatures as voice ----
            priv, pub_pem = _ed25519_keypair()
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": f"telnyx sms {tag}", "provider": "telnyx_sms",
                "handler_workflow_id": handler["id"], "config": {"public_key": pub_pem}})
            assert res.status_code == 201, res.text
            ep2 = res.json()
            target = ep2["webhook_url"]  # the server's FULL path - the signature base derives from it
            payload = json.dumps({"data": {"event_type": "message.received", "payload": {
                "id": f"tm-{tag}", "from": {"phone_number": "+234803"},
                "to": [{"phone_number": "+234804"}], "text": "Reply HELP for help"}}}).encode()
            res = await client.post(_wh(target), content=payload,
                                    headers={**_rfc9421_headers(priv, payload, target),
                                             "content-type": "application/json"})
            assert res.status_code == 200, res.text
            out2 = res.json()
            assert out2["received"] == 1 and not out2["skipped"]
            assert out2["handled"][0]["reply"] == "Hi , got: Reply HELP for help"

            # a delivery status rides the SAME signed receiver as a skip
            payload2 = json.dumps({"data": {"event_type": "message.finalized", "payload": {
                "id": f"df-{tag}"}}}).encode()
            res = await client.post(_wh(target), content=payload2,
                                    headers={**_rfc9421_headers(priv, payload2, target),
                                             "content-type": "application/json"})
            assert res.status_code == 200, res.text
            assert res.json()["received"] == 0
            assert res.json()["skipped"][0]["reason"] == "status_update"

            # wrong signature -> 401; the digest binds the body so a
            # tampered payload fails even with a VALID signature
            payload3 = json.dumps({"data": {"event_type": "message.received", "payload": {"text": "x"}}}).encode()
            mangled = _rfc9421_headers(priv, payload3, target)
            mangled["signature"] = "sig1=:bmls:"  # base64('nil') - not a real signature
            res = await client.post(_wh(target), content=payload3,
                                    headers={**mangled, "content-type": "application/json"})
            assert res.status_code == 401
            res = await client.post(_wh(target), content=b"tampered",
                                    headers={**_rfc9421_headers(priv, payload3, target),
                                             "content-type": "application/json"})
            assert res.status_code == 401

            # the SMS transcripts live in the SAME interaction layer
            res = await client.get("/interactions/conversations?channel=sms", headers=h)
            convs = res.json()["conversations"] if "conversations" in res.json() else res.json()
            assert len(convs) >= 2
            channels_used = {c["channel"] for c in convs}
            assert channels_used == {"sms"}

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 3) the voice agent builder: CRUD + scaffold + session inheritance
# ---------------------------------------------------------------------------
def test_v71_voice_agent_builder():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"agent-{tag}", 1)
            h = _auth(user["token"])

            # ---- create with a SCAFFOLDED handler: runnable immediately ----
            res = await client.post("/voice/agents", headers=h, json={
                "name": f"Front Desk {tag}",
                "description": "answers the support line",
                "greeting_text": "Hello, you have reached the front desk.",
                "asr_provider": "py8n_local", "tts_provider": "openai_tts",
                "tts_voice": "nova", "language": "en-US", "barge_in": True,
                "system_prompt": "You are the polite front desk agent.",
                "scaffold_handler": True})
            assert res.status_code == 201, res.text
            agent = res.json()
            assert agent["speech"]["tts_voice"] == "nova"
            assert agent["handler_is_scaffold"] is True and agent["handler_workflow_id"]
            assert agent["wiring"]["media_stream"].startswith("point the provider's audio fork")

            # the scaffolded handler is a REAL workflow and RUNS offline
            res = await client.get(f"/workflows/{agent['handler_workflow_id']}", headers=h)
            assert res.status_code == 200, res.text
            wf = res.json()
            types = [n["type"] for n in wf["graph"]["nodes"]]
            assert types == ["manual_trigger", "code"]
            code = next(n["parameters"]["code"] for n in wf["graph"]["nodes"] if n["type"] == "code")
            assert "Voice agent handler" in code
            res = await client.post(f"/workflows/{wf['id']}/run", headers=h, json={"payload": {
                "text": "I need help", "participant": {"id": "+1"},
                "metadata": {"system_prompt": agent["system_prompt"]}}})
            assert res.status_code == 200, res.text
            ex = res.json()["execution_id"]
            for _ in range(200):
                det = (await client.get(f"/executions/{ex}", headers=h)).json()
                if det["status"] not in ("running", "queued"):
                    break
                await asyncio.sleep(0.05)
            assert det["status"] == "success", det.get("error")
            reply = det["node_runs"][-1]["output"]
            reply_text = reply.get("result", reply)["text"] if isinstance(reply, dict) else str(reply)
            assert "[You are the polite front desk agent" in reply_text
            assert "I need help" in reply_text

            # ---- validation: unknown providers refuse loudly ----------------
            res = await client.post("/voice/agents", headers=h, json={
                "name": "x", "asr_provider": "shazam"})
            assert res.status_code == 400 and "asr provider" in res.json()["detail"]
            res = await client.post("/voice/agents", headers=h, json={
                "name": "x", "tts_provider": "scream"})
            assert res.status_code == 400 and "tts provider" in res.json()["detail"]
            res = await client.post("/voice/agents", headers=h, json={"name": "  "})
            assert res.status_code == 400 and "name" in res.json()["detail"]

            # ---- sessions inherit the agent ---------------------------------
            res = await client.post("/voice/sessions", headers=h, json={
                "direction": "inbound", "provider": "telnyx", "call_ref": f"CA{tag}",
                "from_ref": "sip:caller@sip.test", "to_ref": "sip:frontdesk@sip.test",
                "agent_id": agent["id"]})
            assert res.status_code == 201, res.text
            sess = res.json()
            assert sess["agent"]["voice_agent_id"] == agent["id"]
            assert sess["agent"]["greeting_text"].startswith("Hello, you have reached")
            assert sess["agent"]["asr_provider"] == "py8n_local"
            assert sess["handler_workflow_id"] == agent["handler_workflow_id"]  # the fallback
            assert sess["conversation_id"]

            # the linked conversation answers through the agent's handler too
            res = await client.get(f"/interactions/conversations/{sess['conversation_id']}", headers=h)
            assert res.json()["handler_workflow_id"] == agent["handler_workflow_id"]

            # ---- the greeting rides call.answered ---------------------------
            res = await client.post(f"/voice/sessions/{sess['id']}/events", headers=h,
                                    json={"kind": "call.ringing"})
            assert res.status_code == 200 and res.json()["greeting_tts"] is None
            res = await client.post(f"/voice/sessions/{sess['id']}/events", headers=h,
                                    json={"kind": "call.answered"})
            assert res.status_code == 200, res.text
            answered = res.json()
            g = answered["greeting_tts"]
            assert g["text"] == "Hello, you have reached the front desk."
            assert g["provider"] == "openai_tts" and g["voice"] == "nova"
            assert g["barge_in_ok"] is True and g["tts_id"]
            detail = (await client.get(f"/voice/sessions/{sess['id']}", headers=h)).json()
            assert detail["active_tts"] is True
            kinds = [e["kind"] for e in detail["events"]]
            assert "tts.started" in kinds

            # barge-in cancels the greeting (the SAME primitive)
            res = await client.post(f"/voice/sessions/{sess['id']}/barge-in", headers=h)
            assert res.status_code == 200 and res.json()["barge_in_count"] == 1

            # ---- turns use the agent's TTS config without per-call params ---
            res = await client.post(f"/voice/sessions/{sess['id']}/turn", headers=h,
                                    json={"transcript": "What are your hours?",
                                          "voice": "alloy"})  # the explicit parameter wins
            assert res.status_code == 200, res.text
            turn = res.json()
            assert turn["tts"]["provider"] == "openai_tts"
            assert turn["tts"]["voice"] == "alloy"  # the explicit parameter
            assert turn["reply"] == "[You are the polite front desk agent] You said: What are your hours?"
            # complete the utterance, then a turn with NO explicit params:
            await client.post(f"/voice/sessions/{sess['id']}/tts/complete", headers=h)
            res = await client.post(f"/voice/sessions/{sess['id']}/turn", headers=h,
                                    json={"transcript": "thanks"})
            assert res.json()["tts"]["voice"] == "nova"  # back to the agent's voice

            # ---- ownership: a stranger's agent is 404 everywhere ------------
            stranger = await _mk_user(client, f"agent-{tag}", 2)
            res = await client.get(f"/voice/agents/{agent['id']}", headers=_auth(stranger["token"]))
            assert res.status_code == 404
            res = await client.post("/voice/sessions", headers=_auth(stranger["token"]),
                                    json={"agent_id": agent["id"]})
            assert res.status_code == 400 and "not found" in res.json()["detail"]

            # an unknown agent id is the same honest error
            res = await client.post("/voice/sessions", headers=h, json={"agent_id": "nope"})
            assert res.status_code == 400 and "not found" in res.json()["detail"]

            # ---- update: greeting + barge-in flip ---------------------------
            res = await client.put(f"/voice/agents/{agent['id']}", headers=h,
                                   json={"greeting_text": "Welcome back.", "barge_in": False})
            assert res.status_code == 200, res.text
            updated = res.json()
            assert updated["greeting_text"] == "Welcome back." and updated["speech"]["barge_in"] is False

            # a NEW session copies the NEW config; the greeting is then
            # non-interruptible and on_answered respects the flag
            res = await client.post("/voice/sessions", headers=h, json={
                "agent_id": agent["id"], "call_ref": f"CA2{tag}"})
            sess2 = res.json()
            assert sess2["agent"]["barge_in"] is False  # copied at creation
            await client.post(f"/voice/sessions/{sess2['id']}/events", headers=h,
                              json={"kind": "call.answered"})
            detail2 = (await client.get(f"/voice/sessions/{sess2['id']}", headers=h)).json()
            g_ev = next(e for e in detail2["events"] if e["kind"] == "tts.started")
            assert g_ev["payload"]["source"] == "greeting" and g_ev["payload"]["barge_in_ok"] is False
            assert g_ev["payload"]["text"] == "Welcome back."

            # the first session KEEPS its creation-time config (never rewritten)
            res = await client.get(f"/voice/sessions/{sess['id']}", headers=h)
            assert res.json()["agent"]["greeting_text"] == "Hello, you have reached the front desk."

            # ---- list + delete ------------------------------------------------
            res = await client.get("/voice/agents", headers=h)
            assert any(a["id"] == agent["id"] for a in res.json()["agents"])
            res = await client.delete(f"/voice/agents/{agent['id']}", headers=h)
            assert res.status_code == 200
            res = await client.get(f"/voice/agents/{agent['id']}", headers=h)
            assert res.status_code == 404
            # the scaffolded handler workflow survives (member objects survive deletes)
            res = await client.get(f"/workflows/{wf['id']}", headers=h)
            assert res.status_code == 200

            # a no-agent session reports agent=None (pre-v71 shape intact)
            res = await client.post("/voice/sessions", headers=h, json={"call_ref": f"CA3{tag}"})
            assert res.status_code == 201 and res.json()["agent"] is None

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 4) the voice agent on the media stream: engine selection + barge-in
# ---------------------------------------------------------------------------
def test_v71_voice_agent_media_stream():
    tag = uuid.uuid4().hex[:8]
    calls: dict[str, int] = {"agent_engine": 0, "other_engine": 0}

    def agent_engine(pcm: bytes, sample_rate: int) -> dict:
        calls["agent_engine"] += 1
        return {"transcript": "I heard it through the agent engine", "confidence": 0.9,
                "language": "en", "is_final": True}

    def other_engine(pcm: bytes, sample_rate: int) -> dict:
        calls["other_engine"] += 1
        return {"transcript": "wrong engine", "confidence": 0.9, "language": "en", "is_final": True}

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"stream-{tag}", 1)
            h = _auth(user["token"])

            # NO explicit handler: the agent's scaffold answers
            res = await client.post("/voice/agents", headers=h, json={
                "name": f"Stream Agent {tag}", "greeting_text": "Hi! How can I help?",
                "asr_provider": "py8n_local", "tts_provider": "piper_local",
                "tts_voice": "en_US-amy", "system_prompt": "You are a phone agent.",
                "scaffold_handler": True})
            assert res.status_code == 201, res.text
            agent = res.json()

            transport.register_asr_engine("py8n_local", agent_engine)
            transport.register_asr_engine("override_engine", other_engine)
            try:
                res = await client.post("/voice/sessions", headers=h, json={
                    "agent_id": agent["id"], "provider": "telnyx",
                    "call_ref": f"CAws{tag}", "from_ref": "+1", "to_ref": "+2"})
                sess = res.json()
                res = await client.post(f"/voice/sessions/{sess['id']}/events", headers=h,
                                        json={"kind": "call.answered"})
                assert res.status_code == 200 and res.json()["greeting_tts"]["text"] == "Hi! How can I help?"

                ws = _WSClient(app, f"/api/v1/voice/sessions/{sess['id']}/media")
                await ws.connect()
                connected = json.loads(await ws.receive_text())
                # the connected frame REPORTS the agent + the engine it will use
                assert connected["agent"]["id"] == agent["id"]
                assert connected["agent"]["name"] == f"Stream Agent {tag}"
                assert connected["asr_engine"] == "py8n_local"
                assert connected["asr_engine_registered"] is True

                await ws.send_text(json.dumps({"event": "start", "start": {
                    "streamSid": f"SS{tag}", "customParameters": {
                        "encoding": "linear16", "sample_rate": 8000}}}))
                assert json.loads(await ws.receive_text())["event"] == "stream_started"

                # the caller SPEAKS OVER THE GREETING: barge-in over the stream
                await ws.send_text(_media(LOUD_200MS, 1))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "barge_in" and ev["interrupted"]
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "speech.started"

                # silence closes the utterance -> ASR through the AGENT'S engine
                for i in (2, 3, 4):
                    await ws.send_text(_media(SILENT_200MS, i))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "speech.ended"
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "asr.final"
                assert ev["asr"]["transcript"] == "I heard it through the agent engine"
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "turn"
                assert calls == {"agent_engine": 1, "other_engine": 0}
                # the turn's TTS rides the AGENT's config (piper_local / en_US-amy)
                assert ev["tts"]["provider"] == "piper_local"
                assert ev["tts"]["voice"] == "en_US-amy"
                assert ev["reply"].startswith("[You are a phone agent]")

                # the stream's customParameters OVERRIDE the agent's engine -
                # a second start frame (providers restart streams) carries
                # asr_engine=override_engine; the caller also speaks over the
                # first turn's utterance -> the SAME barge-in primitive fires
                await ws.send_text(json.dumps({"event": "start", "start": {
                    "streamSid": f"SS{tag}b", "customParameters": {
                        "encoding": "linear16", "sample_rate": 8000,
                        "asr_engine": "override_engine"}}}))
                ack = json.loads(await ws.receive_text())
                assert ack["event"] == "stream_started" and ack["stream_sid"] == f"SS{tag}b"
                for i in range(5, 8):
                    await ws.send_text(_media(SILENT_200MS, i))
                await ws.send_text(_media(LOUD_200MS, 8))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "barge_in" and ev["barge_in_count"] == 2
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "speech.started"
                for i in (9, 10, 11):
                    await ws.send_text(_media(SILENT_200MS, i))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "speech.ended"
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "asr.final"
                assert ev["asr"]["transcript"] == "wrong engine"
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "turn"
                assert calls == {"agent_engine": 1, "other_engine": 1}

                await ws.send_text(json.dumps({"event": "stop"}))
                ev = json.loads(await ws.receive_text())
                assert ev["event"] == "stream_stopped"
                await ws.close()

                # the session's timeline keeps the whole agent story
                detail = (await client.get(f"/voice/sessions/{sess['id']}", headers=h)).json()
                kinds = [e["kind"] for e in detail["events"]]
                assert kinds.count("asr.final") == 2
                assert detail["turn_count"] == 2 and detail["barge_in_count"] == 2
                assert detail["agent"]["tts_voice"] == "en_US-amy"
                # the transcript: greeting barge-in + two turns, ONE conversation
                res = await client.get(f"/interactions/conversations/{detail['conversation_id']}", headers=h)
                roles = [(m["role"], m["channel"]) for m in res.json()["messages"]
                         if m["role"] in ("user", "agent")]
                assert roles == [("user", "voice"), ("agent", "voice")] * 2
            finally:
                transport.unregister_asr_engine("py8n_local")
                transport.unregister_asr_engine("override_engine")

            # the asr.unavailable honesty also names the agent's provider
            res = await client.post("/voice/agents", headers=h, json={
                "name": f"Deaf Agent {tag}", "asr_provider": "deepgram",
                "scaffold_handler": True})
            agent2 = res.json()
            assert agent2["speech"]["asr_engine_registered"] is False
            assert "deepgram" in agent2["wiring"]["asr_note"]
            res = await client.post("/voice/sessions", headers=h, json={"agent_id": agent2["id"]})
            sess2 = res.json()
            await client.post(f"/voice/sessions/{sess2['id']}/events", headers=h,
                              json={"kind": "call.answered"})
            ws = _WSClient(app, f"/api/v1/voice/sessions/{sess2['id']}/media")
            await ws.connect()
            connected = json.loads(await ws.receive_text())
            assert connected["asr_engine"] == "deepgram"
            assert connected["asr_engine_registered"] is False
            await ws.close()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
