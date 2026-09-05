"""V76 feature tests: queueing and waiting on the CHANNEL side (a waiting
room that holds live calls in on_hold, derives FIFO positions and wait
times, and seats the head into a destination meeting ON THE SAME CALL),
voicemail DROPS triggered by greeting_end (the campaign policy that leaves
the configured message on the machine and hangs up, the session-level drop
primitive, the carrier's greeting_ended webhook walk), the room's GROUP
CHAT (member/moderator posts, the agent answering on the asking leg's
conversation), and the MODERATOR's speaking queue (raise hand -> call next
grants the floor through the same directed-floor primitive).

Runs the FastAPI app in-process (httpx ASGITransport); no network egress.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.main import app
from app.services import executor as executor_mod

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
        "email": f"v76-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v76 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_agent(client: httpx.AsyncClient, headers: dict, name: str,
                    greeting: str = "Welcome to the room.") -> dict:
    res = await client.post("/voice/agents", headers=headers, json={
        "name": name, "scaffold_handler": True, "greeting_text": greeting})
    assert res.status_code == 201, res.text
    return res.json()


async def _mk_meeting(client: httpx.AsyncClient, headers: dict, agent_id: str,
                      labels: tuple[str, ...] = ()) -> tuple[dict, list[dict]]:
    res = await client.post("/voice/meetings", headers=headers,
                            json={"title": "v76 room", "agent_id": agent_id})
    assert res.status_code == 201, res.text
    meeting = res.json()
    legs = []
    for label in labels:
        res = await client.post(f"/voice/meetings/{meeting['id']}/join",
                                headers=headers, json={"label": label})
        assert res.status_code == 200, res.text
        legs.append(res.json()["participant"])
    res = await client.get(f"/voice/meetings/{meeting['id']}", headers=headers)
    return res.json(), legs


async def _mk_live_session(client: httpx.AsyncClient, headers: dict,
                           frm: str = "+15550001111") -> dict:
    """An answered inbound call (in_progress) - queueable."""
    res = await client.post("/voice/sessions", headers=headers, json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"cc-{frm}",
        "from_ref": frm, "to_ref": "+15550002222"})
    assert res.status_code == 201, res.text
    sid = res.json()["id"]
    res = await client.post(f"/voice/sessions/{sid}/events", headers=headers,
                            json={"kind": "call.ringing", "payload": {}})
    assert res.status_code == 200, res.text
    res = await client.post(f"/voice/sessions/{sid}/events", headers=headers,
                            json={"kind": "call.answered", "payload": {}})
    assert res.status_code == 200, res.text
    res = await client.get(f"/voice/sessions/{sid}", headers=headers)
    return res.json()


# ---------------------------------------------------------------------------
# 1) channel queues: hold, FIFO, seat into a meeting, leave, full/closed
# ---------------------------------------------------------------------------


def test_v76_channel_queue():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "queue")
            h = _auth(user["token"])

            # a queue with a destination meeting
            agent = await _mk_agent(client, h, "queue room persona")
            meeting, _legs = await _mk_meeting(client, h, agent["id"])
            res = await client.post("/voice/queues", headers=h, json={
                "name": "support line", "meeting_id": meeting["id"],
                "config": {"max_size": 2, "max_wait_seconds": 600}})
            assert res.status_code == 201, res.text
            queue = res.json()
            assert queue["state"] == "open"
            assert queue["config"] == {"max_size": 2, "max_wait_seconds": 600}
            assert queue["depth"]["waiting"] == 0

            # validation: name required, unknown meeting refused
            res = await client.post("/voice/queues", headers=h, json={"name": "  "})
            assert res.status_code == 400 and "name" in res.json()["detail"]
            res = await client.post("/voice/queues", headers=h, json={
                "name": "ghost", "meeting_id": "no-such-meeting"})
            assert res.status_code == 400 and "not found" in res.json()["detail"]

            # two live calls join the line - the FIRST held session
            s1 = await _mk_live_session(client, h, "+15550001111")
            s2 = await _mk_live_session(client, h, "+15550003333")
            assert s1["state"] == "in_progress"
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": s1["id"], "label": "Alice"})
            assert res.status_code == 201, res.text
            assert res.json()["entry_id"]
            # enqueue HOLDS the call (the state machine's own waiting state)
            res = await client.get(f"/voice/sessions/{s1['id']}", headers=h)
            assert res.json()["state"] == "on_hold"
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": s2["id"], "label": "Bob"})
            assert res.status_code == 201, res.text

            # derived FIFO positions + honest depth
            res = await client.get(f"/voice/queues/{queue['id']}", headers=h)
            body = res.json()
            assert body["depth"]["waiting"] == 2
            by_label = {e["label"]: e for e in body["entries"]}
            assert by_label["Alice"]["position"] == 1
            assert by_label["Bob"]["position"] == 2
            assert by_label["Alice"]["waited_seconds"] is not None
            assert by_label["Alice"]["abandoned"] is False

            # duplicates and meeting legs refuse loudly
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": s1["id"]})
            assert res.status_code == 400 and "already waiting" in res.json()["detail"]
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": "ghost"})
            assert res.status_code == 400
            ringing_only = await client.post("/voice/sessions", headers=h, json={
                "direction": "inbound", "provider": "telnyx", "from_ref": "+15550004444"})
            rid = ringing_only.json()["id"]
            await client.post(f"/voice/sessions/{rid}/events", headers=h,
                              json={"kind": "call.ringing", "payload": {}})
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": rid})
            assert res.status_code == 400 and "in_progress" in res.json()["detail"]

            # the full queue refuses (max_size 2)
            s3 = await _mk_live_session(client, h, "+15550005555")
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": s3["id"]})
            assert res.status_code == 400 and "full" in res.json()["detail"]

            # leaving releases the call back to in_progress
            bob_entry = by_label["Bob"]["id"]
            res = await client.post(f"/voice/queues/{queue['id']}/entries/{bob_entry}/leave",
                                    headers=h)
            assert res.status_code == 200, res.text
            res = await client.get(f"/voice/sessions/{s2['id']}", headers=h)
            assert res.json()["state"] == "in_progress"  # released, not dropped

            # seating the head: released AND attached to the destination
            # meeting as a leg - the SAME call, now in the room
            res = await client.post(f"/voice/queues/{queue['id']}/next", headers=h,
                                    json={})
            assert res.status_code == 200, res.text
            seat = res.json()
            assert seat["seated"]["label"] == "Alice"
            assert seat["seated"]["released_state"] == "in_progress"
            assert seat["attached"]["meeting_id"] == meeting["id"]
            res = await client.get(f"/voice/sessions/{s1['id']}", headers=h)
            assert res.json()["state"] == "in_progress"
            res = await client.get(f"/voice/meetings/{meeting['id']}", headers=h)
            room = res.json()
            seated_legs = [p for p in room["participants"]
                           if p["session_id"] == s1["id"]]
            assert seated_legs and seated_legs[0]["state"] == "joined"

            # an abandoned head (caller hung up while waiting) is skipped
            s4 = await _mk_live_session(client, h, "+15550006666")
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": s4["id"], "label": "Cara"})
            assert res.status_code == 201, res.text
            await client.post(f"/voice/sessions/{s4['id']}/events", headers=h,
                              json={"kind": "hangup", "payload": {"reason": "gone"}})
            res = await client.get(f"/voice/queues/{queue['id']}", headers=h)
            body = res.json()
            cara = next(e for e in body["entries"] if e["label"] == "Cara")
            assert cara["abandoned"] is True
            assert cara["position"] is None  # abandoned callers hold no place
            assert body["depth"]["abandoned"] == 1
            res = await client.post(f"/voice/queues/{queue['id']}/next", headers=h, json={})
            assert res.status_code == 400
            assert "hung up" in res.json()["detail"] or "nobody" in res.json()["detail"]

            # closed queues take nobody; unknown queue 404s
            res = await client.post(f"/voice/queues/{queue['id']}/state", headers=h,
                                    json={"state": "closed"})
            assert res.status_code == 200 and res.json()["state"] == "closed"
            s5 = await _mk_live_session(client, h, "+15550007777")
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": s5["id"]})
            assert res.status_code == 400 and "closed" in res.json()["detail"]
            res = await client.get("/voice/queues/no-such", headers=h)
            assert res.status_code == 404

            # a stranger's queue is invisible (owner-scoped)
            stranger = await _mk_user(client, "queue", 2)
            res = await client.get(f"/voice/queues/{queue['id']}",
                                   headers=_auth(stranger["token"]))
            assert res.status_code == 404

    assert _sync(_wrap(_go())) is None


# ---------------------------------------------------------------------------
# 2) voicemail drops: config validation, the drop primitive, the campaign
#    greeting_end walk (service + API), the simulate door
# ---------------------------------------------------------------------------


def test_v76_voicemail_drops():
    async def _go():
        from app.services.channel_adapters import telnyx_parse_webhook

        # the carrier's greeting_ended verdict maps to greeting_end; the
        # plain machine verdicts keep their v70 mapping
        g = telnyx_parse_webhook({"data": {"event_type": "call.machine.detection.ended",
                                           "payload": {"call_control_id": "cc-1",
                                                       "result": "greeting_ended"}}})
        assert g.events and g.events[0].kind == "greeting_end"
        m = telnyx_parse_webhook({"data": {"event_type": "call.machine.detection.ended",
                                           "payload": {"call_control_id": "cc-1",
                                                       "result": "machine_greeting"}}})
        assert m.events and m.events[0].kind == "voicemail_detected"

        async with _client() as client:
            user = await _mk_user(client, "drop")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "dialer persona")

            # drop policy validation: needs greeting_end mode + a message
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "bad drop", "agent_id": agent["id"],
                "targets": [{"address": "+15556660001"}],
                "config": {"amd": {"mode": "detect", "on_machine": "voicemail_drop",
                                   "voicemail_message": "hi"}}})
            assert res.status_code == 400 and "greeting_end" in res.json()["detail"]
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "silent drop", "agent_id": agent["id"],
                "targets": [{"address": "+15556660001"}],
                "config": {"amd": {"mode": "greeting_end", "on_machine": "voicemail_drop"}}})
            assert res.status_code == 400 and "voicemail_message" in res.json()["detail"]
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "novel drop", "agent_id": agent["id"],
                "targets": [{"address": "+15556660001"}],
                "config": {"amd": {"mode": "greeting_end", "on_machine": "voicemail_drop",
                                   "voicemail_message": "x" * 601}}})
            assert res.status_code == 400 and "600" in res.json()["detail"]

            # a real drop campaign
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "renewal drops", "agent_id": agent["id"],
                "targets": [{"address": "+15556660001", "name": "machine line"}],
                "config": {"amd": {"mode": "greeting_end", "on_machine": "voicemail_drop",
                                   "voicemail_message":
                                       "Hi, calling about your renewal. Goodbye."}}})
            assert res.status_code == 201, res.text
            camp = res.json()
            assert camp["config"]["amd"]["voicemail_message"].startswith("Hi,")

            # the session-level door: a live call walks greeting_end + drop
            sess = await _mk_live_session(client, h)
            sid = sess["id"]
            # a drop without the signal is refused - py8n never talks over a human
            res = await client.post(f"/voice/sessions/{sid}/greeting-end", headers=h,
                                    json={})
            # greeting_end alone IS legal from in_progress (the signal lands,
            # state -> voicemail) - with NO message, no drop runs
            assert res.status_code == 200, res.text
            assert res.json()["state"] == "voicemail"
            assert "drop" not in res.json()
            # a drop message on a NON-voicemail call refuses loudly (hold
            # takes it out of in_progress first)
            other = await _mk_live_session(client, h, "+15550008888")
            res = await client.post(f"/voice/sessions/{other['id']}/events", headers=h,
                                    json={"kind": "hold", "payload": {}})
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/sessions/{other['id']}/greeting-end",
                                    headers=h, json={"message": "x"})
            assert res.status_code == 400 and "voicemail" in res.json()["detail"]
            # a SECOND greeting_end is legal from voicemail (the carrier may
            # report twice) - and this time the drop runs and CLOSES the call
            res = await client.post(f"/voice/sessions/{sid}/greeting-end", headers=h,
                                    json={"message": "Sorry we missed you."})
            assert res.status_code == 200, res.text
            body = res.json()
            # the top-level state is the PRE-drop state; the drop carries
            # the ending (the primitive hangs up after the utterance)
            assert body["state"] == "voicemail"
            assert body["drop"]["state"] == "ended"
            assert body["drop"]["end_reason"] == "voicemail_drop"
            assert body["drop"]["message"] == "Sorry we missed you."
            assert body["drop"]["tts"]["barge_in_ok"] is False
            assert body["drop"]["tts"]["tts_id"]
            res = await client.get(f"/voice/sessions/{sid}", headers=h)
            timeline = res.json()
            drop_events = [e for e in timeline["events"]
                           if e["kind"] == "tts.started"
                           and e["payload"].get("source") == "voicemail_drop"]
            assert drop_events, "the drop utterance is on the timeline"
            assert timeline["end_reason"] == "voicemail_drop"
            # an ended call takes nothing more
            res = await client.post(f"/voice/sessions/{sid}/greeting-end", headers=h,
                                    json={"message": "again"})
            assert res.status_code == 400, res.text

            # the campaign simulate walk with as_machine='greeting_end'
            target = camp["targets"][0]
            res = await client.post(
                f"/voice/campaigns/{camp['id']}/targets/{target['id']}/simulate-answer",
                headers=h, json={"as_machine": "greeting_end"})
            assert res.status_code == 200, res.text
            walk = res.json()
            assert walk["simulated"] is True
            assert walk["amd"]["drop"] is True
            assert walk["amd"]["drop_record"]["message"].startswith("Hi,")
            assert walk["amd"]["target_status"] == "voicemail"
            assert walk["amd"]["session_end_reason"] == "voicemail_drop"
            res = await client.get(f"/voice/campaigns/{camp['id']}", headers=h)
            camp2 = res.json()
            tgt = camp2["targets"][0]
            assert tgt["status"] == "voicemail"
            assert tgt["amd"]["result"] == "greeting_ended"
            assert tgt["voicemail_drop"]["tts_id"]
            assert camp2["progress"]["counts"]["voicemail"] == 1

            # invalid as_machine values refuse loudly
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "second line", "agent_id": agent["id"],
                "targets": [{"address": "+15556660002"}],
                "config": {"amd": {"mode": "greeting_end", "on_machine": "hangup"}}})
            assert res.status_code == 201, res.text
            camp3 = res.json()
            res = await client.post(
                f"/voice/campaigns/{camp3['id']}/targets/{camp3['targets'][0]['id']}/simulate-answer",
                headers=h, json={"as_machine": "psychic"})
            assert res.status_code == 400
            assert "as_machine must be" in res.json()["detail"]
            # the hangup policy under greeting_end still hangs up on the beep
            res = await client.post(
                f"/voice/campaigns/{camp3['id']}/targets/{camp3['targets'][0]['id']}/simulate-answer",
                headers=h, json={"as_machine": "greeting_end"})
            assert res.status_code == 200, res.text
            assert res.json()["amd"]["hangup"] is True
            assert res.json()["amd"]["session_end_reason"] == "answering_machine"

    assert _sync(_wrap(_go())) is None


# ---------------------------------------------------------------------------
# 3) the receiver: a signed greeting_ended webhook drives the drop on a
#    campaign call (commands built and honestly skipped without keys)
# ---------------------------------------------------------------------------


def test_v76_receiver_greeting_end_drop():
    import base64
    import hashlib

    from app.services import voice_campaigns as camp_svc
    from app.db import AsyncSessionLocal

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

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "rcv")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "receiver persona")
            priv, pub_pem = _ed25519_keypair()
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "rcv telnyx", "provider": "telnyx_call_control",
                "config": {"api_key": "", "connection_id": "conn-76",
                           "webhook_url": "https://py8n.example/api/v1/channels/telnyx/x/webhook",
                           "from_number": "+15559996", "public_key": pub_pem}})
            assert res.status_code == 201, res.text
            ep = res.json()
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "drop walk", "agent_id": agent["id"], "endpoint_id": ep["id"],
                "targets": [{"address": "+15556661000", "name": "answering machine"}],
                "config": {"amd": {"mode": "greeting_end", "on_machine": "voicemail_drop",
                                   "voicemail_message": "This is py8n. Goodbye."}}})
            assert res.status_code == 201, res.text
            camp = res.json()

            # the dial rides machine_detection: greeting_end (built, honestly
            # skipped - no api_key), then the target is started
            res = await client.post(f"/voice/campaigns/{camp['id']}/start", headers=h)
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["targets"][0]["status"] == "skipped"  # no api_key: honest
            # walk the target through the answered path first
            res = await client.post(
                f"/voice/campaigns/{camp['id']}/targets/{camp['targets'][0]['id']}/simulate-answer",
                headers=h)
            assert res.status_code == 200, res.text
            # the session is live; now the carrier's SIGNED webhook lands
            # greeting_ended and the receiver drives the real drop. The
            # carrier identifies the CALL - the simulate gave it a real
            # call_control_id (call_ref), which the webhook carries.
            res = await client.get(f"/voice/sessions/{res.json()['session_id']}", headers=h)
            call_ref = res.json()["call_ref"]
            assert call_ref.startswith("sim-")

            from app.services.channel_endpoints import receive_voice_webhook
            from app.models import ChannelEndpoint, VoiceSession

            async with AsyncSessionLocal() as db:
                ep_row = await db.get(ChannelEndpoint, ep["id"])
                assert ep_row is not None
                cc = call_ref
                client_state = camp_svc.client_state_for(camp["id"], camp["targets"][0]["id"])
                payload = {"data": {"event_type": "call.machine.detection.ended",
                                    "payload": {"call_control_id": cc,
                                                "client_state": client_state,
                                                "result": "greeting_ended"}}}
                raw = json.dumps(payload).encode()
                target_url = "/api/v1/channels/telnyx/x/webhook"
                out = await receive_voice_webhook(db, ep_row,
                                                  raw_body=raw,
                                                  headers=_rfc9421_headers(priv, raw, target_url),
                                                  method="POST", target=target_url)
                await db.commit()
                assert out["ok"] is True
                handled = out["handled"][0]
                actions = handled["actions"]
                assert "vm_drop_speak_built" in actions, actions
                assert "vm_drop_hangup_built" in actions, actions
                sid = handled["session_id"]
                session = await db.get(VoiceSession, sid)
                assert session.state == "ended"
                assert session.end_reason == "voicemail_drop"
                # the target booked the drop
                from app.models import VoiceCampaignTarget

                target = await db.get(VoiceCampaignTarget, camp["targets"][0]["id"])
                await db.refresh(target)
                assert target.status == "voicemail"
                assert (target.meta or {}).get("voicemail_drop", {}).get(
                    "message") == "This is py8n. Goodbye."

    assert _sync(_wrap(_go())) is None


# ---------------------------------------------------------------------------
# 4) the room's group chat: member/moderator posts, ask_agent answers on
#    the leg's conversation, inactive rooms refuse
# ---------------------------------------------------------------------------


def test_v76_meeting_chat():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "chat")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "chat persona")
            meeting, legs = await _mk_meeting(client, h, agent["id"], ("Alice",))
            alice = legs[0]

            # a member posts
            res = await client.post(f"/voice/meetings/{meeting['id']}/chat", headers=h,
                                    json={"text": "what are your hours?",
                                          "participant_id": alice["id"]})
            assert res.status_code == 200, res.text
            msg = res.json()["message"]
            assert msg["role"] == "member" and msg["author"] == "Alice"
            assert msg["participant_id"] == alice["id"]

            # the moderator posts
            res = await client.post(f"/voice/meetings/{meeting['id']}/chat", headers=h,
                                    json={"text": "welcome everyone", "author": "Mo"})
            assert res.status_code == 200, res.text
            assert res.json()["message"]["role"] == "moderator"

            # ask_agent: the agent answers ON Alice's leg - the reply lands
            # in the chat AND on her linked conversation (meeting_chat)
            res = await client.post(f"/voice/meetings/{meeting['id']}/chat", headers=h,
                                    json={"text": "say the welcome line",
                                          "participant_id": alice["id"], "ask_agent": True})
            assert res.status_code == 200, res.text
            reply = res.json().get("agent_reply")
            assert reply and reply["role"] == "agent"
            assert reply["meta"]["in_reply_to"] == res.json()["message"]["id"]

            res = await client.get(f"/voice/meetings/{meeting['id']}/chat", headers=h)
            log = res.json()["messages"]
            # the three human posts in order, then the agent's reply
            assert [m["text"] for m in log[:3]] == ["what are your hours?",
                                                    "welcome everyone",
                                                    "say the welcome line"]
            assert log[3]["role"] == "agent"
            assert log[3]["meta"]["in_reply_to"] == log[2]["id"]

            # the leg's conversation carries BOTH sides under meeting_chat
            res = await client.get(f"/voice/sessions/{alice['session_id']}", headers=h)
            conv_id = res.json()["conversation_id"]
            res = await client.get(f"/interactions/conversations/{conv_id}", headers=h)
            conv = res.json()
            chat_msgs = [m for m in conv["messages"] if m.get("channel") == "meeting_chat"] \
                if isinstance(conv, dict) and conv.get("messages") else []
            assert chat_msgs, "the chat rode the leg's conversation"
            assert chat_msgs[0]["role"] == "user"

            # validation: empty text, foreign participant, moderator
            # ask_agent (no leg), ended rooms
            res = await client.post(f"/voice/meetings/{meeting['id']}/chat", headers=h,
                                    json={"text": "   "})
            assert res.status_code == 400 and "text" in res.json()["detail"]
            res = await client.post(f"/voice/meetings/{meeting['id']}/chat", headers=h,
                                    json={"text": "hi", "participant_id": "ghost"})
            assert res.status_code == 400 and "not found" in res.json()["detail"]
            res = await client.post(f"/voice/meetings/{meeting['id']}/chat", headers=h,
                                    json={"text": "hi", "ask_agent": True})
            assert res.status_code == 400 and "MEMBER" in res.json()["detail"]
            res = await client.post("/voice/meetings/no-such/chat", headers=h,
                                    json={"text": "hi"})
            assert res.status_code == 400 and "not found" in res.json()["detail"]
            res = await client.get("/voice/meetings/no-such/chat", headers=h)
            assert res.status_code == 404

            # an ended room takes no messages
            await client.post(f"/voice/meetings/{meeting['id']}/end", headers=h)
            res = await client.post(f"/voice/meetings/{meeting['id']}/chat", headers=h,
                                    json={"text": "anyone there?"})
            assert res.status_code == 400 and "ended" in res.json()["detail"]

            # the meeting detail carries the chat count
            res = await client.get(f"/voice/meetings/{meeting['id']}", headers=h)
            assert res.json()["counts"]["chat_messages"] == 4

    assert _sync(_wrap(_go())) is None


# ---------------------------------------------------------------------------
# 5) the moderator's speaking queue: raise, FIFO, call next grants the
#    floor, duplicate/absent hands refuse
# ---------------------------------------------------------------------------


def test_v76_moderator_hand_queue():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "hand")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "standup persona")
            meeting, legs = await _mk_meeting(client, h, agent["id"],
                                              ("Alice", "Bob", "Cara"))
            alice, bob, cara = legs

            # raise two hands (with a note), queue keeps FIFO order
            res = await client.post(f"/voice/meetings/{meeting['id']}/hand", headers=h,
                                    json={"participant_id": alice["id"], "note": "question"})
            assert res.status_code == 200, res.text
            assert res.json()["hand_queue"]["count"] == 1
            res = await client.post(f"/voice/meetings/{meeting['id']}/hand", headers=h,
                                    json={"participant_id": bob["id"]})
            assert res.status_code == 200, res.text
            hq = res.json()["hand_queue"]
            assert [e["label"] for e in hq["entries"]] == ["Alice", "Bob"]
            assert hq["entries"][0]["position"] == 1
            assert hq["entries"][0]["note"] == "question"
            assert hq["entries"][0]["waited_seconds"] is not None

            # duplicates refuse loudly
            res = await client.post(f"/voice/meetings/{meeting['id']}/hand", headers=h,
                                    json={"participant_id": alice["id"]})
            assert res.status_code == 400 and "already" in res.json()["detail"]
            # non-joined legs refuse
            res = await client.post(f"/voice/meetings/{meeting['id']}/hand", headers=h,
                                    json={"participant_id": "ghost"})
            assert res.status_code == 400 and "not found" in res.json()["detail"]

            # the meeting detail carries the queue; GET /hand returns it too
            res = await client.get(f"/voice/meetings/{meeting['id']}", headers=h)
            assert res.json()["hand_queue"]["count"] == 2
            res = await client.get(f"/voice/meetings/{meeting['id']}/hand", headers=h)
            assert res.json()["hand_queue"]["count"] == 2

            # call next: Alice gets the FLOOR (directed) and leaves the queue
            res = await client.post(f"/voice/meetings/{meeting['id']}/hand/next", headers=h)
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["called"] == alice["id"]
            assert out["floor"]["mode"] == "directed"
            assert out["floor"]["participant_id"] == alice["id"]
            assert out["hand_queue"]["count"] == 1
            assert out["hand_queue"]["entries"][0]["label"] == "Bob"

            # lower Bob's hand (moderator declined)
            res = await client.delete(f"/voice/meetings/{meeting['id']}/hand/{bob['id']}",
                                      headers=h)
            assert res.status_code == 200 and res.json()["hand_queue"]["count"] == 0
            res = await client.delete(f"/voice/meetings/{meeting['id']}/hand/{bob['id']}",
                                      headers=h)
            assert res.status_code == 400 and "not in the speaking queue" in res.json()["detail"]

            # empty queue refuses to call next
            res = await client.post(f"/voice/meetings/{meeting['id']}/hand/next", headers=h)
            assert res.status_code == 400 and "nobody" in res.json()["detail"]

            # Cara raises, the room ends - the queue refuses after
            res = await client.post(f"/voice/meetings/{meeting['id']}/hand", headers=h,
                                    json={"participant_id": cara["id"]})
            assert res.status_code == 200, res.text
            await client.post(f"/voice/meetings/{meeting['id']}/end", headers=h)
            res = await client.post(f"/voice/meetings/{meeting['id']}/hand/next", headers=h)
            assert res.status_code == 400 and "ended" in res.json()["detail"]

            # a stranger's meeting queue is invisible
            stranger = await _mk_user(client, "hand", 2)
            res = await client.get(f"/voice/meetings/{meeting['id']}/hand",
                                   headers=_auth(stranger["token"]))
            assert res.status_code == 404

    assert _sync(_wrap(_go())) is None


# ---------------------------------------------------------------------------
# 6) platform pins: version moved, node catalog unchanged
# ---------------------------------------------------------------------------


def test_v76_version_pin():
    from app.config import settings

    assert settings.version == "1.76.0"
