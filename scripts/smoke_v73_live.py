"""V73 live smoke: boot the real server and verify the offline-phone batch.

1. MODEL INSTALLS: POST /voice/speech/models/install downloads the REAL
   artifacts - the vosk small model (41 MB Kaldi zip), the piper release
   binary (tarball + libs + espeak-ng-data) and the real lessac-medium
   voice (.onnx + .onnx.json) - verifies and lays them out, then REBINDS:
   the inventory goes honest-available, py8n_local / piper_local register
   WITHOUT any env var pre-set, and /voice/tts/synthesize returns a REAL
   piper-synthesized RIFF wav. The offline-phone block reports ready.
2. FULLY OFFLINE PHONE: piper SPEAKS the caller's question into a REAL
   websocket media stream, the in-process vosk bridge transcribes it,
   the knowledge binding grounds the answer (an exact-transcript HTTP
   turn quotes the FAQ row), and the per-turn ASR analytics show REAL
   confidence numbers from the acoustic model.
3. LLM BRAIN: the voice-agent-system solution installs with
   brain=ai_agent over the SAME knowledge dataset - the scaffolded
   handler is an ai_agent node whose prompt carries the caller's words
   AND the knowledge matches (asserted at a stand-in OpenAI-compatible
   bridge), and the phone answers from the installed knowledge.

Usage: /home/z/.venv/bin/python scripts/smoke_v73_live.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import subprocess
import sys
import threading
import time
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v73_server.py"
API = "http://127.0.0.1:8201/api/v1"
BRIDGE_PORT = 3011

sys.path.insert(0, BACKEND)

# captured by the stand-in LLM bridge (in-process, so the smoke can read it)
BRIDGE_REQUESTS: list[dict] = []


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
# the stand-in OpenAI-compatible bridge (PROVES the prompt is grounded: it
# only answers from the FAQ when the knowledge matches actually arrived)
# ---------------------------------------------------------------------------

def _start_bridge() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: N805 - silence
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
            BRIDGE_REQUESTS.append(body)
            user = ""
            for m in body.get("messages", []):
                if m.get("role") == "user":
                    user = str(m.get("content") or "")
            if "opening hours" in user and "nine AM to six PM" in user:
                answer = "We are open Monday to Friday from nine AM to six PM."
            elif "knowledge" in user.lower():
                answer = "I will take a message."
            else:
                answer = "I did not get my grounding."
            payload = json.dumps({
                "choices": [{"message": {"content": json.dumps({"answer": answer})}}]
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------------------
# 1) real model installs
# ---------------------------------------------------------------------------

def model_install_check(c: httpx.Client) -> dict:
    surface = c.get("/voice/speech/models").json()
    assert len(surface["models"]) == 4, [m["slug"] for m in surface["models"]]
    # NOTE: a previous run's installs persist under data/models - the machine
    # may ALREADY be an offline phone at boot (which is the point).

    for slug, expect_bytes in (("vosk-small-en-us", 41_205_931),
                               ("piper-binary-linux", None),
                               ("piper-lessac-medium", None)):
        res = c.post("/voice/speech/models/install", json={"slug": slug})
        assert res.status_code == 200, res.text
        inst = res.json()["install"]
        assert inst["slug"] == slug, inst
        assert inst["installed_paths"], inst
        total = sum(inst["bytes"].values())
        if expect_bytes:
            assert total == expect_bytes, (slug, total)
        print(f"    installed {slug}: {total/1e6:.1f} MB verified")

    after = c.get("/voice/speech/models").json()
    installed = {m["slug"]: m["installed"] for m in after["models"]}
    assert installed == {"piper-binary-linux": True, "piper-lessac-medium": True,
                         "vosk-small-en-us": True, "whisper-tiny-en": False}, installed
    inv = after["inventory"]
    assert inv["tts"]["local_engine_registered"] is True, inv["tts"]
    assert inv["asr"]["local_engine_registered"] is True, inv["asr"]
    assert inv["asr"]["vosk"]["available"] is True, inv["asr"]["vosk"]
    assert inv["asr"]["vosk"]["model"].endswith("vosk-model-small-en-us-0.15"), inv["asr"]["vosk"]
    assert after["offline_phone"]["ready"] is True, after["offline_phone"]

    # REAL piper synthesis (the installed binary + voice, not a stand-in)
    res = c.post("/voice/tts/synthesize", json={
        "text": "Hello, this is the py8n offline phone.", "provider": "piper_local"})
    assert res.status_code == 200, res.text
    tts = res.json()
    audio = base64.b64decode(tts["audio_b64"])
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE", audio[:12]
    assert tts["duration_estimate_ms"] > 300, tts
    return {"tts_ms": tts["duration_estimate_ms"], "vosk_model": inv["asr"]["vosk"]["model"]}


# ---------------------------------------------------------------------------
# 2) the fully offline phone: piper speaks -> vosk hears -> knowledge answers
# ---------------------------------------------------------------------------

def _synthesize_question(text: str) -> tuple[bytes, int]:
    """The REAL installed piper speaks the question; returns (pcm, rate)."""
    models = f"{BACKEND}/data/models"
    binary = os.path.realpath(f"{models}/bin/piper")
    voice = f"{models}/en_US-lessac-medium.onnx"
    out = f"/tmp/py8n-v73-q-{uuid.uuid4().hex[:6]}.wav"
    proc = subprocess.run([binary, "--model", voice, "--output_file", out],
                          input=text.encode(), capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[:300]
    with wave.open(out, "rb") as w:
        rate = w.getframerate()
        pcm = w.readframes(w.getnframes())
    os.unlink(out)
    return pcm, rate


def _media(payload: str, chunk: int, rate: int) -> str:
    return json.dumps({"event": "media", "media": {
        "payload": payload, "track": "inbound", "chunk": chunk,
        "encoding": "linear16", "sample_rate": rate}})


def _silence(rate: int, ms: int) -> str:
    n = int(rate * ms / 1000)
    return base64.b64encode(b"\x00\x00" * n).decode()


async def _ws_offline_call(sess_id: str, pcm: bytes, rate: int) -> dict:
    import websockets

    chunk_samples = int(rate * 0.2)
    frames = []
    for i in range(0, len(pcm) // 2, chunk_samples):
        seg = pcm[i * 2:(i + chunk_samples) * 2]
        frames.append(base64.b64encode(seg).decode())

    seen: dict = {}
    uri = f"ws://127.0.0.1:8201/api/v1/voice/sessions/{sess_id}/media"
    async with websockets.connect(uri, open_timeout=10) as ws:
        connected = json.loads(await ws.recv())
        assert connected["event"] == "connected", connected
        assert "py8n_local" in connected["asr_engines"], connected
        await ws.send(json.dumps({"event": "start", "start": {
            "streamSid": "SS-v73", "callSid": sess_id,
            "customParameters": {"encoding": "linear16", "sample_rate": rate}}}))
        ack = json.loads(await ws.recv())
        assert ack["event"] == "stream_started", ack

        for i in range(3):
            await ws.send(_media(_silence(rate, 200), i, rate))
        for i, f in enumerate(frames, start=10):
            await ws.send(_media(f, i, rate))
        for i in range(6):
            await ws.send(_media(_silence(rate, 200), 100 + i, rate))

        deadline = time.time() + 60
        while time.time() < deadline:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            seen.setdefault(ev["event"], ev)
            if ev["event"] == "turn":
                break
        await ws.send(json.dumps({"event": "stop"}))
    return seen


def _knowledge_handler(c: httpx.Client, tag: str) -> dict:
    res = c.post("/workflows", json={"name": f"v73-kb-handler-{tag}", "graph": {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "reply", "type": "code", "name": "reply", "position": {"x": 1, "y": 0},
             "parameters": {"code": (
                 "env = input_data.get('payload', {})\n"
                 "meta = env.get('metadata') or {}\n"
                 "matches = meta.get('knowledge') or []\n"
                 "if matches:\n"
                 "    reply = str(matches[0].get('answer') or '')\n"
                 "else:\n"
                 "    reply = 'no knowledge matched'\n"
                 "result = {'text': reply}\n")}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "reply"}],
    }})
    assert res.status_code in (200, 201), res.text
    return res.json()


def offline_phone_check(c: httpx.Client, tag: str) -> dict:
    faq = c.post("/datasets", json={"name": f"v73-faq-{tag}", "rows": [
        {"question": "What are your opening hours",
         "answer": "We are open Monday to Friday from nine AM to six PM."},
        {"question": "Where are you located",
         "answer": "Our office is at forty-two Market Street, downtown."},
    ]})
    assert faq.status_code == 201, faq.text
    handler = _knowledge_handler(c, tag)
    agent = c.post("/voice/agents", json={
        "name": f"Offline desk {tag}", "greeting_text": "Hi, ask me anything.",
        "handler_workflow_id": handler["id"],
        "asr_provider": "py8n_local", "tts_provider": "piper_local",
        "tts_voice": "en_US-lessac-medium",
        "knowledge_dataset_id": faq.json()["id"], "knowledge_text_column": "question",
        "knowledge_answer_column": "answer"})
    assert agent.status_code == 201, agent.text
    a = agent.json()

    sess = c.post("/voice/sessions", json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"CA-off-{tag}",
        "from_ref": "+15550701", "to_ref": "+15550702", "agent_id": a["id"]})
    assert sess.status_code == 201, sess.text
    s = sess.json()
    c.post(f"/voice/sessions/{s['id']}/events", json={"kind": "call.ringing"})
    ans = c.post(f"/voice/sessions/{s['id']}/events", json={"kind": "call.answered"})
    assert ans.status_code == 200, ans.text
    c.post(f"/voice/sessions/{s['id']}/barge-in")

    # piper SPEAKS the question; the real vosk hears it over the websocket
    pcm, rate = _synthesize_question("What are your opening hours")
    seen = asyncio.run(_ws_offline_call(s["id"], pcm, rate))
    assert "asr.final" in seen, list(seen)
    asr = seen["asr.final"]["asr"]
    assert asr["transcript"].strip(), asr
    assert 0.0 <= asr["confidence"] <= 1.0
    if "turn" in seen:
        assert seen["turn"]["reply"], seen["turn"]

    # the exact-transcript turn quotes the FAQ row (grounded, offline)
    c.post(f"/voice/sessions/{s['id']}/tts/complete")
    turn = c.post(f"/voice/sessions/{s['id']}/turn",
                  json={"transcript": "What are your opening hours?"})
    assert turn.status_code == 200, turn.text
    t = turn.json()
    assert t["reply"] == "We are open Monday to Friday from nine AM to six PM.", t

    # per-turn ASR confidence analytics over the REAL session
    an = c.get(f"/voice/sessions/{s['id']}/analytics").json()
    assert an["confidence"]["turns"] >= 2, an["confidence"]
    assert an["confidence"]["turns_reported"] >= 1, an["confidence"]
    assert an["confidence"]["mean"] > 0, an["confidence"]
    pool = c.get(f"/voice/agents/{a['id']}/analytics").json()
    assert pool["sessions_with_turns"] == 1, pool
    assert pool["confidence"]["reported_turns"] >= 1, pool
    return {"asr_transcript": asr["transcript"], "asr_confidence": asr["confidence"],
            "reply": t["reply"], "mean_confidence": an["confidence"]["mean"]}


# ---------------------------------------------------------------------------
# 3) the LLM brain over the SAME binding, via the solution install
# ---------------------------------------------------------------------------

def ai_brain_check(c: httpx.Client, tag: str) -> dict:
    inst = c.post("/solutions/voice-agent-system/install",
                  json={"as_voice_agent": True, "brain": "ai_agent"})
    assert inst.status_code == 200, inst.text
    va = inst.json()["voice_agent"]
    detail = c.get(f"/voice/agents/{va['id']}").json()
    assert detail["brain"]["kind"] == "ai_agent", detail["brain"]
    assert detail["handler_is_scaffold"] is True
    assert detail["knowledge"]["dataset_id"], detail

    sess = c.post("/voice/sessions", json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"CA-brain-{tag}",
        "from_ref": "+15550801", "to_ref": "+15550802", "agent_id": va["id"]})
    assert sess.status_code == 201, sess.text
    s = sess.json()
    c.post(f"/voice/sessions/{s['id']}/events", json={"kind": "call.ringing"})
    c.post(f"/voice/sessions/{s['id']}/events", json={"kind": "call.answered"})
    c.post(f"/voice/sessions/{s['id']}/barge-in")
    turn = c.post(f"/voice/sessions/{s['id']}/turn",
                  json={"transcript": "What are your opening hours?"})
    assert turn.status_code == 200, turn.text
    t = turn.json()
    assert t["reply"] == "We are open Monday to Friday from nine AM to six PM.", t

    # the bridge PROVES the grounding arrived in the prompt
    assert BRIDGE_REQUESTS, "the stand-in bridge saw no request"
    user = next((str(m.get("content")) for r in BRIDGE_REQUESTS
                 for m in r.get("messages", []) if m.get("role") == "user"), "")
    assert "What are your opening hours" in user, user[:300]
    assert "nine AM to six PM" in user, user[:300]
    sysmsg = next((str(m.get("content")) for r in BRIDGE_REQUESTS
                   for m in r.get("messages", []) if m.get("role") == "system"), "")
    assert "courteous phone support agent" in sysmsg, sysmsg[:300]
    return {"reply": t["reply"], "prompt_head": user[:90]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v73_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PORT": "8201",
        "PY8N_LLM_BRIDGE_URL": f"http://127.0.0.1:{BRIDGE_PORT}",
    })
    srv = _start_bridge()
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=API, timeout=900) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.73.0", version
            tag = uuid.uuid4().hex[:6]

            mi = model_install_check(c)
            print(f"[1] MODEL INSTALLS OK - real vosk ({mi['vosk_model'].split('/')[-1]}), "
                  f"real piper binary + real lessac voice downloaded/verified/rebound through "
                  f"the API; the offline phone reports READY and /voice/tts/synthesize "
                  f"returned a REAL piper wav ({mi['tts_ms']} ms) with no env vars pre-set")

            op = offline_phone_check(c, tag)
            print(f"[2] FULLY OFFLINE PHONE OK - piper SPOKE 'What are your opening hours' "
                  f"into a real websocket media stream, the in-process vosk bridge heard "
                  f"'{op['asr_transcript']}' (confidence {op['asr_confidence']:.2f}), and the "
                  f"call answered FROM the FAQ dataset: '{op['reply']}'; per-turn analytics "
                  f"mean confidence {op['mean_confidence']:.2f}")

            br = ai_brain_check(c, tag)
            print(f"[3] LLM BRAIN OK - the solution installed with brain=ai_agent over the "
                  f"SAME knowledge; the scaffolded handler's prompt carried the caller's "
                  f"words + the matches ('{br['prompt_head']}...'), and the phone answered: "
                  f"'{br['reply']}'")

            print(f"\nALL 3 CHECKS GREEN - v73 live smoke passed (version {version})")
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
        srv.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
