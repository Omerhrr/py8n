"""V79 live smoke: boot the real server and drive the recording /
transcription archives, the SMS auto-answer, and the room lifecycle.

1. RECORDING ARCHIVES: a room with two web legs; a recording pass opens
   (every leg's timeline told); REAL linear16 audio streams over a REAL
   media websocket (the VAD closes the utterance, the capture hook eats
   the PCM); chat lines land; STOP writes the archive - the transcript
   markdown artifact (speakers + chat), the chat json artifact and a
   REAL RIFF/WAV artifact of the captured leg (the other leg honestly
   absent); a leg joining mid-recording starts capturing; ending the
   room stops the recording automatically.
2. SMS AUTO-ANSWER: the waiting caller TEXTS "1" through a signed
   generic_sms webhook - the reply is intercepted BEFORE the conversation
   layer: the entry trades the hold for a CALLBACK (reason=callback, the
   composed campaign books the target), the confirmation text goes back
   (honest skipped without credentials), the timeline carries the story;
   a non-keyword text rides the normal conversation path instead.

Usage: /home/z/.venv/bin/python scripts/smoke_v79_live.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v74_server.py"
API = "http://127.0.0.1:8207/api/v1"
WS_BASE = "ws://127.0.0.1:8207/api/v1"
SERVER_PORT = 8207


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


def _sms_headers(secret: str, raw: bytes) -> dict:
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return {"content-type": "application/json",
            "x-py8n-signature": f"sha256={sig}"}


def _mk_live_call(c: httpx.Client, call_ref: str, frm: str) -> dict:
    sess = c.post("/voice/sessions", json={
        "direction": "inbound", "provider": "telnyx", "call_ref": call_ref,
        "from_ref": frm, "to_ref": "+15554440999"}).json()
    c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.ringing", "payload": {}})
    c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.answered", "payload": {}})
    assert c.get(f"/voice/sessions/{sess['id']}").json()["state"] == "in_progress"
    return sess


# ---------------------------------------------------------------------------
# 1) recording / transcription archives - live
# ---------------------------------------------------------------------------

def recordings_check(c: httpx.Client, tag: str) -> dict:
    import websockets

    agent = c.post("/voice/agents", json={
        "name": f"Room persona {tag}", "greeting_text": "",
        "scaffold_handler": True}).json()
    meeting = c.post("/voice/meetings", json={
        "title": f"v79 archive room {tag}", "agent_id": agent["id"]}).json()
    legs = []
    for label in ("Ada", "Bob"):
        leg = c.post(f"/voice/meetings/{meeting['id']}/join",
                     json={"label": label, "channel": "web"}).json()["participant"]
        legs.append(leg)

    # the recording opens: the room is told, capture starts for both legs
    res = c.post(f"/voice/meetings/{meeting['id']}/recordings",
                 json={"name": f"standup {tag}"})
    assert res.status_code == 201, res.text
    rec = res.json()
    assert rec["state"] == "recording"
    assert len(rec["capture_started_sessions"]) == 2, rec
    for leg in legs:
        kinds = [e["kind"] for e in c.get(f"/voice/sessions/{leg['session_id']}").json()["events"]]
        assert "recording.started" in kinds, kinds

    # the room talks: chat + a REAL utterance over Ada's REAL media socket
    c.post(f"/voice/meetings/{meeting['id']}/chat",
           json={"participant_id": legs[0]["id"], "author": "Ada",
                 "text": "agenda anyone?"})
    c.post(f"/voice/meetings/{meeting['id']}/chat",
           json={"participant_id": legs[0]["id"], "author": "Ada",
                 "text": "sharing notes in a second"})

    async def _drive() -> int:
        import base64
        import struct

        async with websockets.connect(
                f"{WS_BASE}/voice/sessions/{legs[0]['session_id']}/media",
                open_timeout=15, close_timeout=10) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert hello["event"] == "connected", hello
            await ws.send(json.dumps({"event": "start", "start": {
                "streamSid": f"py8n-smoke-{tag}", "callSid": legs[0]["session_id"],
                "customParameters": {"encoding": "linear16", "sample_rate": 16000}}}))
            started = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert started["event"] == "stream_started", started

            def _b64(samples: list[int]) -> str:
                return base64.b64encode(struct.pack(f"<{len(samples)}h", *samples)).decode()

            silence = _b64([0] * 3200)    # 200ms @ 16k
            loud = _b64([9000] * 3200)    # 200ms @ 16k, well over the VAD
            for seq, chunk in enumerate((silence, loud, loud, loud,
                                         silence, silence, silence), start=1):
                await ws.send(json.dumps({"event": "media", "media": {
                    "payload": chunk, "track": "inbound", "chunk": seq,
                    "encoding": "linear16", "sample_rate": 16000}}))
            got_ended = 0
            for _ in range(30):
                frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if frame.get("event") == "speech.ended":
                    got_ended += 1
                    break
            assert got_ended == 1, "the VAD never closed the utterance live"
            return got_ended

    utterances = asyncio.run(_drive())
    assert utterances == 1

    # a leg joining a RECORDED room starts capturing on join
    late = c.post(f"/voice/meetings/{meeting['id']}/join",
                  json={"label": "Carol", "channel": "web"}).json()
    assert late["recording"]["capturing"] is True, late

    # STOP: the archive is written
    res = c.post(f"/voice/meetings/{meeting['id']}/recordings/{rec['id']}/stop")
    assert res.status_code == 200, res.text
    archive = res.json()
    assert archive["state"] == "stopped"
    assert archive["counts"]["chat_lines"] == 2, archive["counts"]
    audio = {a["session_id"]: a for a in archive["artifacts"]["audio"]}
    assert audio[legs[0]["session_id"]]["captured"] is True
    assert audio[legs[0]["session_id"]]["utterances"] == 1
    assert audio[legs[0]["session_id"]]["duration_ms"] >= 600.0
    assert audio[legs[1]["session_id"]]["captured"] is False
    assert audio[late["participant"]["session_id"]]["captured"] is False

    # the transcript artifact is REAL markdown; the leg audio is a REAL wav
    t_id = archive["artifacts"]["transcript"]
    res = c.get(f"/artifacts/{t_id}/content")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/markdown")
    md = res.text
    assert "# standup" in md and "## Transcript" in md and "## Chat" in md
    assert "agenda anyone?" in md
    a_id = audio[legs[0]["session_id"]]["artifact_id"]
    res = c.get(f"/artifacts/{a_id}/content")
    assert res.status_code == 200 and res.headers["content-type"].startswith("audio/wav")
    assert res.content[:4] == b"RIFF" and res.content[8:12] == b"WAVE"
    assert len(res.content) > 44 + 3000, len(res.content)

    # ending the room stops any ACTIVE recording automatically
    res = c.post(f"/voice/meetings/{meeting['id']}/recordings", json={})
    assert res.status_code == 201, res.text
    rec2 = res.json()
    out = c.post(f"/voice/meetings/{meeting['id']}/end").json()
    assert out["state"] == "ended"
    assert out["recording"]["id"] == rec2["id"]
    assert out["recording"]["state"] == "stopped"
    rows = c.get(f"/voice/meetings/{meeting['id']}/recordings").json()["recordings"]
    assert len(rows) == 2 and rows[0]["meta"]["stop_reason"] == "meeting_ended"
    return {"archive": archive["id"], "audio_legs": archive["counts"]["audio_legs"],
            "lines": archive["counts"]["transcript_lines"]}


# ---------------------------------------------------------------------------
# 2) the SMS auto-answer - reply "1" leaves the queue (via callback), live
# ---------------------------------------------------------------------------

def auto_answer_check(c: httpx.Client, tag: str) -> dict:
    agent = c.post("/voice/agents", json={
        "name": f"Line persona {tag}", "greeting_text": "Thanks for holding.",
        "scaffold_handler": True}).json()
    ep = c.post("/channels/endpoints", json={
        "name": f"any-gateway {tag}", "provider": "generic_sms",
        "config": {"secret": f"sk-{tag}"}})
    assert ep.status_code == 201, ep.text
    ep = ep.json()
    queue = c.post("/voice/queues", json={
        "name": f"auto line {tag}", "agent_id": agent["id"],
        "config": {"sms": {"enabled": True, "channel_id": ep["id"],
                           "auto_answer": {"enabled": True, "keyword": "1",
                                           "action": "callback"}},
                   "callback": {"enabled": True}}}).json()
    assert queue["config"]["sms"]["auto_answer"]["enabled"] is True

    session = _mk_live_call(c, f"cc-auto-{tag}", "+15554440777")
    res = c.post(f"/voice/queues/{queue['id']}/entries",
                 json={"session_id": session["id"]})
    assert res.status_code == 201, res.text
    entry_id = res.json()["entry_id"]

    # the caller TEXTS "1" - signed any-gateway webhook
    body = json.dumps({"from": "+15554440777", "to": "+15554440999",
                       "text": " 1 "}).encode()
    res = c.post(f"/channels/sms/{ep['id']}/webhook", content=body,
                 headers=_sms_headers(f"sk-{tag}", body))
    assert res.status_code == 200, res.text
    out = res.json()
    handled = out["handled"][0]
    assert handled["auto_answer"] is True, handled
    assert handled["action"]["done"] is True and handled["action"]["action"] == "callback"
    assert "off hold" in handled["reply"] and "call you back" in handled["reply"]
    assert handled["delivery"]["delivery"] == "skipped"  # no credentials: honest

    qd = c.get(f"/voice/queues/{queue['id']}").json()
    assert qd["depth"]["callback"] == 1 and qd["depth"]["waiting"] == 0, qd["depth"]
    assert qd["callback_entries"][0]["id"] == entry_id
    assert qd["callbacks"]["requested"] == 1
    detail = c.get(f"/voice/sessions/{session['id']}").json()
    assert detail["state"] == "ended" and detail["end_reason"] == "callback"
    kinds = [e["kind"] for e in detail["events"]]
    assert "queue.sms" in kinds
    campaign_id = qd["callbacks"]["campaign_id"]
    assert c.get(f"/voice/campaigns/{campaign_id}").json()["progress"]["total"] == 1

    # a NON-keyword text from a waiting caller rides the conversation layer
    session2 = _mk_live_call(c, f"cc-auto2-{tag}", "+15554440888")
    c.post(f"/voice/queues/{queue['id']}/entries", json={"session_id": session2["id"]})
    body2 = json.dumps({"from": "+15554440888", "text": "how long?"}).encode()
    res = c.post(f"/channels/sms/{ep['id']}/webhook", content=body2,
                 headers=_sms_headers(f"sk-{tag}", body2))
    handled2 = res.json()["handled"][0]
    assert "auto_answer" not in handled2, handled2
    assert handled2.get("conversation_id")
    return {"reply": handled["reply"][:60], "campaign": campaign_id[:8],
            "entry_status": qd["callback_entries"][0]["status"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v79_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PORT": str(SERVER_PORT),
    })
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.79.0", version
            tag = uuid.uuid4().hex[:6]

            r = recordings_check(c, tag)
            print(f"[1] RECORDING ARCHIVES OK - the archive {r['archive'][:8]} holds "
                  f"{r['lines']} transcript line(s) + chat + {r['audio_legs']} captured "
                  f"leg audio as REAL artifacts (RIFF/WAV verified), the utterance "
                  f"flowed through the REAL media websocket and the VAD, a leg joined "
                  f"mid-recording and was captured from arrival, and ending the room "
                  f"stopped the active recording automatically")

            a = auto_answer_check(c, tag)
            print(f"[2] SMS AUTO-ANSWER OK - the waiting caller replied \"1\" through a "
                  f"SIGNED webhook: the hold ended (reason=callback), the composed "
                  f"campaign {a['campaign']} booked the target, the confirmation text "
                  f"was sent honestly skipped (\"{a['reply']}...\"), and a non-keyword "
                  f"text rode the normal conversation path")

            print(f"\nALL 2 CHECKS GREEN - v79 live smoke passed (version {version})")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        for f in (db_path, db_path + "-wal", db_path + "-shm"):
            try:
                os.unlink(f)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
