"""V75 feature tests: the room's MIX + FLOOR controls (per-member
muted/deafened/solo enforced at the conversation pipeline, directed/auto
floor gating non-holder turns while the room still transcribes) and the
campaign's RETRY SCHEDULES (retry_on outcomes schedule the next attempt,
the retry pass dials what is due and reports deferred/exhausted) +
ANSWERING MACHINE DETECTION (the dial carries machine_detection, the
carrier's verdict marks the target and the on_machine policy hangs up).

Runs the FastAPI app in-process (httpx ASGITransport); dials are
sender-injected so the wire shape is asserted without network egress.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import httpx
import pytest

from app.main import app
from app.services import executor as executor_mod

API = "http://testserver/api/v1"


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    """The suite runs >100 register/login calls inside the sliding window;
    this file sits last in the alphabet, so the auth bucket is already
    full when it starts. Reset the documented test switch before and after
    each v75 scenario (the limiter itself is covered by its own tests)."""
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
        "email": f"v75-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v75 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_agent(client: httpx.AsyncClient, headers: dict, name: str) -> dict:
    res = await client.post("/voice/agents", headers=headers, json={
        "name": name, "scaffold_handler": True,
        "greeting_text": "Welcome to the room."})
    assert res.status_code == 201, res.text
    return res.json()


async def _mk_meeting(client: httpx.AsyncClient, headers: dict, agent_id: str,
                      labels: tuple[str, ...]) -> tuple[dict, list[dict]]:
    """A meeting with one joined web leg per label; returns (meeting, legs)."""
    res = await client.post("/voice/meetings", headers=headers,
                            json={"title": "mix room", "agent_id": agent_id})
    assert res.status_code == 201, res.text
    meeting = res.json()
    legs = []
    for label in labels:
        res = await client.post(f"/voice/meetings/{meeting['id']}/join",
                                headers=headers, json={"label": label, "channel": "web"})
        assert res.status_code == 200, res.text
        legs.append(res.json()["participant"])
    return meeting, legs


# ---------------------------------------------------------------------------
# 1) per-member mix controls
# ---------------------------------------------------------------------------

def test_v75_meeting_mix_controls():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "mix", 1)
            stranger = await _mk_user(client, "mix", 2)
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "mix persona")
            meeting, legs = await _mk_meeting(client, h, agent["id"], ("alice", "bob"))

            # mix blocks are visible with honest defaults
            detail = (await client.get(f"/voice/meetings/{meeting['id']}", headers=h)).json()
            assert all(p["mix"] == {"muted": False, "deafened": False, "solo": False}
                       for p in detail["participants"])

            # nothing to set refuses loudly
            res = await client.patch(
                f"/voice/meetings/{meeting['id']}/participants/{legs[0]['id']}/mix",
                headers=h, json={})
            assert res.status_code == 400, res.text

            # MUTE alice: her words are recorded as mix.gated ONLY - no
            # asr.final on the leg, no agent reply, nothing in the meeting
            # transcript (the room does not hear her)
            res = await client.patch(
                f"/voice/meetings/{meeting['id']}/participants/{legs[0]['id']}/mix",
                headers=h, json={"muted": True})
            assert res.status_code == 200, res.text
            assert res.json()["participant"]["mix"]["muted"] is True

            res = await client.post(f"/voice/sessions/{legs[0]['session_id']}/turn",
                                    headers=h, json={"transcript": "alice whispers",
                                                     "confidence": 0.9})
            assert res.status_code == 200, res.text
            turn = res.json()
            assert turn["gated"] is True and turn["reason"] == "muted"
            assert turn["reply"] is None and turn["tts"] is None

            sess = (await client.get(f"/voice/sessions/{legs[0]['session_id']}",
                                     headers=h)).json()
            kinds = [e["kind"] for e in sess["events"]]
            assert "mix.gated" in kinds and "asr.final" not in kinds

            detail = (await client.get(f"/voice/meetings/{meeting['id']}", headers=h)).json()
            assert [l["speaker"] for l in detail["transcript"]
                    if l["side"] == "participant"] == [], "muted audio never reaches the room"

            # unmute: her audio flows again
            res = await client.patch(
                f"/voice/meetings/{meeting['id']}/participants/{legs[0]['id']}/mix",
                headers=h, json={"muted": False})
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/sessions/{legs[0]['session_id']}/turn",
                                    headers=h, json={"transcript": "alice asks about the bill",
                                                     "confidence": 0.9})
            assert res.status_code == 200, res.text
            assert "gated" not in res.json(), "unmuted audio flows ungated"
            assert res.json()["reply"], "the handler answered"

            # DEAFEN bob: the agent listens but the reply is withheld
            sess = (await client.get(f"/voice/sessions/{legs[1]['session_id']}",
                                     headers=h)).json()
            tts_before = sum(1 for e in sess["events"] if e["kind"] == "tts.started")
            res = await client.patch(
                f"/voice/meetings/{meeting['id']}/participants/{legs[1]['id']}/mix",
                headers=h, json={"deafened": True})
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/sessions/{legs[1]['session_id']}/turn",
                                    headers=h, json={"transcript": "bob asks about shipping",
                                                     "confidence": 0.9})
            assert res.status_code == 200, res.text
            turn = res.json()
            assert turn["reply"], "the agent still listened"
            assert turn["reply_withheld"], "the reply is withheld"
            assert turn["tts"] is None, "no TTS opens on a deafened leg"
            sess = (await client.get(f"/voice/sessions/{legs[1]['session_id']}",
                                     headers=h)).json()
            assert "asr.final" in [e["kind"] for e in sess["events"]], "the room heard bob"
            tts_after = sum(1 for e in sess["events"] if e["kind"] == "tts.started")
            assert tts_after == tts_before, \
                "no NEW tts opened (the greeting predates the deafening)"

            # SOLO is exclusive: spotlighting alice clears bob's solo
            res = await client.patch(
                f"/voice/meetings/{meeting['id']}/participants/{legs[0]['id']}/mix",
                headers=h, json={"solo": True})
            assert res.status_code == 200, res.text
            res = await client.patch(
                f"/voice/meetings/{meeting['id']}/participants/{legs[1]['id']}/mix",
                headers=h, json={"solo": True})
            assert res.status_code == 200, res.text
            detail = (await client.get(f"/voice/meetings/{meeting['id']}", headers=h)).json()
            mixes = {p["label"]: p["mix"] for p in detail["participants"]}
            assert mixes["alice"]["solo"] is False, "spotlight moved off alice"
            assert mixes["bob"]["solo"] is True

            # solo on alice gates bob's input (reason solo), the room still
            # hears him (asr.final stays) - wait, solo moved to bob; give
            # it back to alice and check bob is gated
            res = await client.patch(
                f"/voice/meetings/{meeting['id']}/participants/{legs[0]['id']}/mix",
                headers=h, json={"solo": True})
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/sessions/{legs[1]['session_id']}/turn",
                                    headers=h, json={"transcript": "bob interjects",
                                                     "confidence": 0.9})
            assert res.status_code == 200, res.text
            turn = res.json()
            assert turn["gated"] is True and turn["reason"] == "solo"
            sess = (await client.get(f"/voice/sessions/{legs[1]['session_id']}",
                                     headers=h)).json()
            assert "asr.final" in [e["kind"] for e in sess["events"]], "the room still heard bob"

            # a stranger cannot touch the room's mix (refused with the reason)
            sh = _auth(stranger["token"])
            res = await client.patch(
                f"/voice/meetings/{meeting['id']}/participants/{legs[0]['id']}/mix",
                headers=sh, json={"muted": True})
            assert res.status_code == 400 and "not found" in res.json()["detail"], res.text
            return

    _sync(_wrap(_go()))


def test_v75_meeting_floor_control():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "floor", 1)
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "floor persona")
            meeting, legs = await _mk_meeting(client, h, agent["id"], ("host", "guest"))

            # auto is the default (v74 behavior)
            detail = (await client.get(f"/voice/meetings/{meeting['id']}", headers=h)).json()
            assert detail["floor"]["mode"] == "auto"
            assert "auto:" in detail["floor"]["note"]

            # directed floor without a participant refuses
            res = await client.post(f"/voice/meetings/{meeting['id']}/floor",
                                    headers=h, json={"mode": "directed"})
            assert res.status_code == 400, res.text

            # give the floor to the HOST: the guest is still transcribed
            # (the room hears) but the agent does not turn on them
            res = await client.post(f"/voice/meetings/{meeting['id']}/floor",
                                    headers=h, json={"mode": "directed",
                                                     "participant_id": legs[0]["id"]})
            assert res.status_code == 200, res.text
            body = res.json()["meeting"]
            assert body["floor"]["mode"] == "directed"
            assert body["floor"]["label"] == "host"
            assert body["floor"]["participant_id"] == legs[0]["id"]
            assert body["floor"]["since"]

            # the holder's turn runs
            res = await client.post(f"/voice/sessions/{legs[0]['session_id']}/turn",
                                    headers=h, json={"transcript": "host asks the agenda",
                                                     "confidence": 0.9})
            assert res.status_code == 200, res.text
            assert res.json()["reply"], "the floor holder's audio triggers turns"

            # a non-holder's turn is gated - but transcribed
            res = await client.post(f"/voice/sessions/{legs[1]['session_id']}/turn",
                                    headers=h, json={"transcript": "guest tries to interject",
                                                     "confidence": 0.9})
            assert res.status_code == 200, res.text
            turn = res.json()
            assert turn["gated"] is True and turn["reason"] == "floor"
            assert turn["reply"] is None
            detail = (await client.get(f"/voice/meetings/{meeting['id']}", headers=h)).json()
            guests = [l for l in detail["transcript"]
                      if l["side"] == "participant" and l["speaker"] == "guest"]
            assert guests and "interject" in guests[0]["text"], \
                "the room still transcribes a gated speaker"

            # release the floor back to auto: the guest turns again
            res = await client.post(f"/voice/meetings/{meeting['id']}/floor",
                                    headers=h, json={"mode": "auto"})
            assert res.status_code == 200, res.text
            assert res.json()["meeting"]["floor"]["mode"] == "auto"
            res = await client.post(f"/voice/sessions/{legs[1]['session_id']}/turn",
                                    headers=h, json={"transcript": "guest tries again",
                                                     "confidence": 0.9})
            assert res.status_code == 200, res.text
            assert res.json()["reply"], "auto floor ungates everyone"

            # the floor holder must be a JOINED leg - after the room ends
            # the controls refuse
            res = await client.post(f"/voice/meetings/{meeting['id']}/end", headers=h)
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/meetings/{meeting['id']}/floor",
                                    headers=h, json={"mode": "auto"})
            assert res.status_code == 400, res.text
            return

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2) campaign retry schedules
# ---------------------------------------------------------------------------

def test_v75_campaign_retry_schedule():
    import app.models as models
    import app.services.voice_campaigns as camp_svc
    from app.db import AsyncSessionLocal

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "retry", 1)
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "retry persona")

            # config validation fails loud
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "bad retry", "agent_id": agent["id"],
                "targets": [{"address": "+15557770001"}],
                "config": {"retry": {"max_attempts": 12}}})
            assert res.status_code == 400, res.text
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "bad amd", "agent_id": agent["id"],
                "targets": [{"address": "+15557770001"}],
                "config": {"amd": {"mode": "psychic"}}})
            assert res.status_code == 400, res.text

            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "retry telnyx", "provider": "telnyx_call_control",
                "config": {"api_key": "telnyx-key-retry",
                           "public_key": "-----BEGIN PUBLIC KEY-----\nMFow\n-----END PUBLIC KEY-----",
                           "connection_id": "conn-retry",
                           "webhook_url": "https://py8n.example/api/v1/channels/telnyx/x/webhook",
                           "from_number": "+15559997"}})
            ep = res.json()
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "renewals with retry", "agent_id": agent["id"],
                "endpoint_id": ep["id"],
                "targets": [{"address": "+15557770001", "name": "dana"},
                            {"address": "+15557770002", "name": "eli"}],
                "config": {"retry": {"max_attempts": 2,
                                     "delays_minutes": [30, 120],
                                     "retry_on": ["no_answer"]}}})
            assert res.status_code == 201, res.text
            camp = res.json()
            # the config was normalized (defaults filled for amd)
            assert camp["config"]["retry"]["max_attempts"] == 2
            assert camp["config"]["retry"]["retry_on"] == ["no_answer"]
            assert camp["config"]["amd"]["mode"] == "disabled"

            dials: list[dict] = []

            async def _sender(config, request):
                dials.append(request)
                return {"status_code": 200,
                        "json": {"data": {"call_control_id": f"CC-retry-{len(dials)}"}}}

            async with AsyncSessionLocal() as db:
                out = await camp_svc.start_campaign(db, user["id"], camp["id"],
                                                    sender=_sender)
                await db.commit()
            assert out["status"] == "running"
            assert all(t["status"] == "dialing" for t in out["targets"])
            assert len(dials) == 2

            # BOTH calls hit no_answer through the webhook-side linkage:
            # the outcome schedules the next attempt at now+30m (attempt 1
            # -> delays_minutes[0])
            async with AsyncSessionLocal() as db:
                for t in out["targets"]:
                    link = await camp_svc.on_call_event(
                        db, call_control_id=t["call_control_id"],
                        event_kind="no_answer")
                    assert link and link["status"] == "no_answer"
                await db.commit()

            async with AsyncSessionLocal() as db:
                rows = {t.address: t for t in (
                    await db.execute(select_targets(camp["id"]))).scalars().all()}
                for t in rows.values():
                    assert t.status == "no_answer"
                    assert t.meta.get("retry_at"), "the next attempt is scheduled"
                    assert t.meta.get("retry_delay_minutes") == 30
                    assert t.attempts == 1

            # the campaign reports the retry picture honestly
            res = await client.get(f"/voice/campaigns/{camp['id']}", headers=h)
            progress = res.json()["progress"]
            assert progress["retry"]["eligible"] == 2
            assert progress["retry"]["due"] == 0, "the 30-minute delay has not passed"
            assert progress["counts"]["no_answer"] == 2

            # a retry pass NOW defers both (not yet due)
            res = await client.post(f"/voice/campaigns/{camp['id']}/retry",
                                    headers=h, json={})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["retry_pass"]["deferred"] == 2
            assert body["retry_pass"]["dialed"] == 0
            assert "deferred" in body["retry_note"]

            # a no-answer dial is never retried when attempts cap is 1:
            # sanity-check the exhaustion branch on a second campaign
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "one shot", "agent_id": agent["id"],
                "endpoint_id": ep["id"],
                "targets": [{"address": "+15557770003"}],
                "config": {"retry": {"max_attempts": 1}}})
            one = res.json()
            async with AsyncSessionLocal() as db:
                await camp_svc.start_campaign(db, user["id"], one["id"], sender=_sender)
                await db.commit()
                row = (await db.execute(
                    select_targets(one["id"]))).scalars().all()[0]
                row.status = "dialing"
                db.add(row)
                await db.commit()
                await camp_svc.on_call_event(db, call_control_id=row.call_control_id,
                                             event_kind="no_answer")
                await db.commit()
                row = (await db.get(models.VoiceCampaignTarget, row.id))
                assert row.status == "no_answer"
                assert row.meta.get("retry_done") is True
                assert "retry_at" not in row.meta, "capped targets never reschedule"
            res = await client.get(f"/voice/campaigns/{one['id']}", headers=h)
            assert res.json()["progress"]["retry"]["exhausted"] == 1
            res = await client.post(f"/voice/campaigns/{one['id']}/retry",
                                    headers=h, json={})
            body = res.json()
            assert body["retry_pass"]["dialed"] == 0
            assert body["retry_pass"]["exhausted"] == 1, "the capped target is counted"
            assert "exhausted (attempts cap)" in body["retry_note"]

            # force ignores the schedule (the manual override says so) -
            # through the service with an injected sender so the dial is
            # really placed; the pass limit leaves the rest honestly counted
            async with AsyncSessionLocal() as db:
                out = await camp_svc.retry_due(db, user["id"], camp["id"],
                                               force=True, limit=1, sender=_sender)
                await db.commit()
            assert out["retry_pass"]["forced"] is True
            assert out["retry_pass"]["dialed"] == 1
            assert out["retry_pass"]["beyond_limit"] == 1, "the clipped one is counted"
            assert "beyond the pass limit" in out["retry_note"]
            # 2 (first start) + 1 (one-shot start) + 1 (forced retry)
            assert len(dials) == 4

            # retry refuses on a stopped campaign
            await client.post(f"/voice/campaigns/{camp['id']}/stop", headers=h)
            res = await client.post(f"/voice/campaigns/{camp['id']}/retry",
                                    headers=h, json={"force": True})
            assert res.status_code == 400, res.text
            return

    _sync(_wrap(_go()))


def select_targets(campaign_id: str):
    from sqlalchemy import select
    from app.models import VoiceCampaignTarget

    return (select(VoiceCampaignTarget)
            .where(VoiceCampaignTarget.campaign_id == campaign_id)
            .order_by(VoiceCampaignTarget.created_at.asc()))


# RFC 9421 signing (mirrors the v70 test helpers - telnyx_verify_signature)
def _ed25519_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub_pem


def _rfc9421_headers(priv, raw: bytes, target: str, *, method: str = "POST") -> dict:
    components = ("@method", "@target", "content-digest")
    covered = " ".join(f'"{c}"' for c in components)
    sig_input = f'sig1=({covered});created=1618884473;keyid="k1"'
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode()
    lines = [f'"@method": {method}', f'"@target": {target}',
             f'"content-digest": sha-256=:{digest}:']
    lines.append(f'"@signature-params": ({covered});created=1618884473;keyid="k1"')
    sig = base64.b64encode(priv.sign("\n".join(lines).encode("utf-8"))).decode()
    return {"signature-input": sig_input, "signature": f"sig1=:{sig}:",
            "content-digest": f"sha-256=:{digest}:"}


# ---------------------------------------------------------------------------
# 3) answering machine detection
# ---------------------------------------------------------------------------

def test_v75_campaign_amd():
    import app.models as models
    import app.services.voice_campaigns as camp_svc
    from app.db import AsyncSessionLocal

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "amd", 1)
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "amd persona")

            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "amd telnyx", "provider": "telnyx_call_control",
                "config": {"api_key": "telnyx-key-amd",
                           "public_key": "-----BEGIN PUBLIC KEY-----\nMFow\n-----END PUBLIC KEY-----",
                           "connection_id": "conn-amd",
                           "webhook_url": "https://py8n.example/api/v1/channels/telnyx/x/webhook",
                           "from_number": "+15559996"}})
            ep = res.json()
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "amd hangup", "agent_id": agent["id"],
                "endpoint_id": ep["id"],
                "targets": [{"address": "+15556660001", "name": "machine"}],
                "config": {"amd": {"mode": "detect", "on_machine": "hangup"}}})
            assert res.status_code == 201, res.text
            camp = res.json()
            # v76: the normalized amd block grew voicemail_message (the drop
            # policy's payload); the v75 keys keep their values
            assert camp["config"]["amd"] == {"mode": "detect", "on_machine": "hangup",
                                             "voicemail_message": ""}

            dials: list[dict] = []

            async def _sender(config, request):
                dials.append(request)
                return {"status_code": 200,
                        "json": {"data": {"call_control_id": f"CC-amd-{len(dials)}"}}}

            async with AsyncSessionLocal() as db:
                await camp_svc.start_campaign(db, user["id"], camp["id"], sender=_sender)
                await db.commit()
            # the dial CARRIES the carrier's AMD mode
            assert dials[0]["json"]["machine_detection"] == "detect"

            res = await client.get(f"/voice/campaigns/{camp['id']}", headers=h)
            target = res.json()["targets"][0]

            # the answered session opens (greeting etc.), THEN the AMD
            # verdict lands and the hangup policy applies
            async def _answer_then_machine():
                state = camp_svc.client_state_for(camp["id"], target["id"])
                async with AsyncSessionLocal() as db:
                    link = await camp_svc.on_call_event(
                        db, call_control_id="CC-amd-1", client_state=state,
                        event_kind="call.answered", session=None)
                    await db.commit()
                    sess = await db.get(models.VoiceSession, link["session_id"])
                    link2 = await camp_svc.on_call_event(
                        db, call_control_id="CC-amd-1", client_state=state,
                        event_kind="voicemail_detected", session=sess)
                    await db.commit()
                return link, link2

            link, link2 = await _answer_then_machine()
            assert link["status"] == "answered" and link["session_id"]
            assert link2["status"] == "voicemail", "the AMD policy marked the target"
            assert link2["amd"] == {"on_machine": "hangup", "hangup": True}

            async def _check():
                async with AsyncSessionLocal() as db:
                    tgt = await db.get(models.VoiceCampaignTarget, target["id"])
                    sess = await db.get(models.VoiceSession, link["session_id"])
                    return tgt.status, tgt.meta.get("amd"), sess.state

            tgt_status, tgt_amd, sess_state = await _check()
            assert tgt_status == "voicemail"
            assert tgt_amd["result"] == "machine" and tgt_amd["mode"] == "detect"
            # the service-level linkage marks the TARGET only - the session
            # side (voicemail state + provider hangup) is the RECEIVER's
            # job, covered in the receiver walk below
            assert sess_state == "in_progress"

            # the campaign counts voicemail in its derived progress
            res = await client.get(f"/voice/campaigns/{camp['id']}", headers=h)
            body = res.json()
            assert body["progress"]["counts"]["voicemail"] == 1
            assert body["progress"]["placed"] == 1

            # on_machine=continue: the verdict is recorded, the conversation lives
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "amd continue", "agent_id": agent["id"],
                "endpoint_id": ep["id"],
                "targets": [{"address": "+15556660002"}],
                "config": {"amd": {"mode": "greeting_end", "on_machine": "continue"}}})
            camp2 = res.json()
            async with AsyncSessionLocal() as db:
                await camp_svc.start_campaign(db, user["id"], camp2["id"], sender=_sender)
                await db.commit()
            res = await client.get(f"/voice/campaigns/{camp2['id']}", headers=h)
            t2 = res.json()["targets"][0]
            assert dials[1]["json"]["machine_detection"] == "greeting_end"

            async def _answer2():
                state = camp_svc.client_state_for(camp2["id"], t2["id"])
                async with AsyncSessionLocal() as db:
                    link = await camp_svc.on_call_event(
                        db, call_control_id="CC-amd-2", client_state=state,
                        event_kind="call.answered", session=None)
                    await db.commit()
                    sess = await db.get(models.VoiceSession, link["session_id"])
                    link2 = await camp_svc.on_call_event(
                        db, call_control_id="CC-amd-2", client_state=state,
                        event_kind="voicemail_detected", session=sess)
                    await db.commit()
                return link, link2

            link, link2 = await _answer2()
            assert link2["status"] == "answered", "continue keeps the conversation"
            assert link2["amd"] == {"on_machine": "continue", "hangup": False}

            # the honest simulate door walks the machine too
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "amd sim", "agent_id": agent["id"],
                "endpoint_id": ep["id"],
                "targets": [{"address": "+15556660003"}],
                "config": {"amd": {"mode": "detect", "on_machine": "hangup"}}})
            camp3 = res.json()
            async with AsyncSessionLocal() as db:
                await camp_svc.start_campaign(db, user["id"], camp3["id"], sender=_sender)
                await db.commit()
            res = await client.get(f"/voice/campaigns/{camp3['id']}", headers=h)
            t3 = res.json()["targets"][0]
            res = await client.post(
                f"/voice/campaigns/{camp3['id']}/targets/{t3['id']}/simulate-answer",
                headers=h, json={"as_machine": True})
            assert res.status_code == 200, res.text
            sim = res.json()
            assert sim["simulated"] is True and sim["amd"]["simulated"] is True
            assert sim["amd"]["hangup"] is True
            assert sim["amd"]["target_status"] == "voicemail"
            assert sim["amd"]["session_state"] == "ended"
            return

    _sync(_wrap(_go()))


def test_v75_dial_amd_param_and_receiver_flow():
    """The dial builder validates the AMD mode; the RECEIVER maps the
    carrier's machine verdict to the session state machine and the
    campaign's policy (hangup attempted honestly, continue recorded)."""
    from app.services.channel_adapters import telnyx_build_dial, telnyx_parse_webhook

    cfg = {"api_key": "k"}
    req = telnyx_build_dial(cfg, to="+15550005", from_ref="+15559999",
                            connection_id="conn-1",
                            webhook_url="https://x.example/hook",
                            machine_detection="detect")
    assert req["json"]["machine_detection"] == "detect"
    req = telnyx_build_dial(cfg, to="+15550005", from_ref="+15559999",
                            connection_id="conn-1",
                            webhook_url="https://x.example/hook",
                            machine_detection="disabled")
    assert "machine_detection" not in req["json"], "disabled sends NO amd param"
    req = telnyx_build_dial(cfg, to="+15550005", from_ref="+15559999",
                            connection_id="conn-1",
                            webhook_url="https://x.example/hook")
    assert "machine_detection" not in req["json"], "omitted stays omitted"
    try:
        telnyx_build_dial(cfg, to="+15550005", from_ref="+15559999",
                          connection_id="conn-1", webhook_url="https://x.example/hook",
                          machine_detection="psychic")
        raise AssertionError("bad mode must fail loud")
    except ValueError as exc:
        assert "machine_detection" in str(exc)

    # the carrier's verdict maps to voicemail_detected (v70 behavior kept)
    parsed = telnyx_parse_webhook({"data": {
        "event_type": "call.machine.detection.ended",
        "payload": {"call_control_id": "CC-x", "result": "machine"}}})
    assert parsed.events and parsed.events[0].kind == "voicemail_detected"

    # the RECEIVER: a full webhook walk for an AMD continue campaign.
    import app.models as models
    import app.services.voice_campaigns as camp_svc
    from app.db import AsyncSessionLocal

    priv, pub_pem = _ed25519_keypair()

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "rcv", 1)
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "receiver persona")
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "rcv telnyx", "provider": "telnyx_call_control",
                "config": {"api_key": "telnyx-key-rcv",
                           "public_key": pub_pem,
                           "connection_id": "conn-rcv",
                           "webhook_url": "https://py8n.example/api/v1/channels/telnyx/x/webhook",
                           "from_number": "+15559995"}})
            ep = res.json()
            res = await client.post("/voice/campaigns", headers=h, json={
                "name": "receiver amd", "agent_id": agent["id"],
                "endpoint_id": ep["id"],
                "targets": [{"address": "+15556660004"}],
                "config": {"amd": {"mode": "detect", "on_machine": "hangup"}}})
            camp = res.json()
            async with AsyncSessionLocal() as db:
                await camp_svc.start_campaign(db, user["id"], camp["id"])
                await db.commit()
            res = await client.get(f"/voice/campaigns/{camp['id']}", headers=h)
            target = res.json()["targets"][0]
            state = camp_svc.client_state_for(camp["id"], target["id"])
            hook_path = f"/api/v1/channels/telnyx/{ep['id']}/webhook"
            post_path = hook_path.removeprefix("/api/v1")  # the client base already carries it

            # answered -> the greeting path runs; AMD -> the policy hangs up
            raw1 = json.dumps({"data": {"event_type": "call.answered",
                                        "payload": {"call_control_id": "CC-rcv-1",
                                                    "client_state": state}}}).encode()
            res = await client.post(post_path, content=raw1,
                                    headers=_rfc9421_headers(priv, raw1, hook_path))
            assert res.status_code == 200, res.text
            raw2 = json.dumps({"data": {"event_type": "call.machine.detection.ended",
                                        "payload": {"call_control_id": "CC-rcv-1",
                                                    "client_state": state,
                                                    "result": "machine"}}}).encode()
            res = await client.post(post_path, content=raw2,
                                    headers=_rfc9421_headers(priv, raw2, hook_path))
            assert res.status_code == 200, res.text
            out = res.json()
            handled = out["handled"][-1]
            assert "amd_hangup_built" in handled["actions"], handled

            async with AsyncSessionLocal() as db:
                tgt = await db.get(models.VoiceCampaignTarget, target["id"])
                sess = await db.get(models.VoiceSession, tgt.session_id)
                return tgt.status, sess.state, sess.end_reason

    tgt_status, sess_state, sess_end = _sync(_wrap(_go()))
    assert tgt_status == "voicemail"
    assert sess_state == "ended" and sess_end == "answering_machine"
