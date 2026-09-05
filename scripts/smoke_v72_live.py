"""V72 live smoke: boot the real server and verify the speech + knowledge batch.

1. SPEECH ENGINES: the machine inventory is honest (vosk/whisper.cpp
   report remediation when absent) AND the REAL piper bridge binds at
   boot through PY8N_PIPER_BIN / PY8N_PIPER_VOICE (a stand-in piper that
   writes a valid wav) - GET /voice/speech/engines reports piper
   available + py8n_local registered, and POST /voice/tts/synthesize
   returns actual RIFF wav bytes with a parsed duration.
2. KNOWLEDGE BINDING: a voice agent bound to an FAQ dataset answers the
   phone FROM the dataset - the search preview shows the grounding, a
   session rings/answers, and a turn quotes the FAQ row back (an
   unmatched question gets the handler's honest fallback line).
3. MARKETPLACE SOLUTION: installing voice-agent-system WITH
   as_voice_agent wires the FAQ dataset + knowledge-grounded handler +
   Voice Agent in one click - a call on the installed agent answers
   from the installed knowledge, fully offline.

Usage: /home/z/.venv/bin/python scripts/smoke_v72_live.py
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
import base64

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v72_server.py"
API = "http://127.0.0.1:8199/api/v1"

sys.path.insert(0, BACKEND)


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


def _make_stand_in_piper(tmp: str) -> tuple[str, str]:
    """A stand-in piper binary: text on stdin -> a real 0.5s wav file."""
    binary = os.path.join(tmp, "piper")
    with open(binary, "w") as fh:
        fh.write(textwrap.dedent("""
            #!/usr/bin/env python3
            import sys, wave
            args = sys.argv[1:]
            out = args[args.index("--output_file") + 1]
            text = sys.stdin.buffer.read().decode("utf-8", "replace")
            frames = 4000 + 200 * len(text)
            with wave.open(out, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes(b"\\x00\\x07" * frames)
        """).strip())
    os.chmod(binary, os.stat(binary).st_mode | stat.S_IEXEC)
    voice = os.path.join(tmp, "en_US-amy-medium.onnx")
    open(voice, "w").close()
    return binary, voice


def _answer_from_knowledge_handler(c: httpx.Client, tag: str) -> dict:
    res = c.post("/workflows", json={"name": f"v72-handler-{tag}", "graph": {
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


def speech_check(c: httpx.Client, tag: str) -> dict:
    inv = c.get("/voice/speech/engines").json()
    assert inv["tts"]["piper"]["available"] is True, inv["tts"]["piper"]
    assert inv["tts"]["local_engine_registered"] is True, inv["tts"]
    assert inv["asr"]["local_engine_registered"] is True, inv["asr"]
    if not inv["asr"]["vosk"]["available"]:
        assert "pip install vosk" in inv["asr"]["vosk"]["note"]
    res = c.post("/voice/tts/synthesize", json={
        "text": "Hello, this is the py8n phone agent.", "provider": "piper_local"})
    assert res.status_code == 200, res.text
    tts = res.json()
    audio = base64.b64decode(tts["audio_b64"])
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE", audio[:12]
    assert tts["duration_estimate_ms"] > 0, tts
    # an unregistered engine keeps refusing loudly
    res = c.post("/voice/tts/synthesize", json={"text": "hi", "provider": "nope"})
    assert res.status_code == 409 and "no TTS engine is registered" in res.json()["detail"]
    return {"duration": tts["duration_estimate_ms"], "vosk_note": inv["asr"]["vosk"]["note"]}


def knowledge_check(c: httpx.Client, tag: str) -> dict:
    faq = c.post("/datasets", json={"name": f"v72-faq-{tag}", "rows": [
        {"question": "What are your opening hours",
         "answer": "We are open Monday to Friday from nine AM to six PM."},
        {"question": "Where are you located",
         "answer": "Our office is at forty-two Market Street, downtown."},
        {"question": "How do I return a product",
         "answer": "You can return any product within thirty days with the receipt."},
    ]})
    assert faq.status_code == 201, faq.text
    ds = faq.json()
    handler = _answer_from_knowledge_handler(c, tag)
    agent = c.post("/voice/agents", json={
        "name": f"FAQ desk {tag}", "greeting_text": "Hi, ask me anything.",
        "handler_workflow_id": handler["id"],
        "knowledge_dataset_id": ds["id"], "knowledge_text_column": "question",
        "knowledge_answer_column": "answer", "knowledge_top_k": 1})
    assert agent.status_code == 201, agent.text
    a = agent.json()
    assert a["knowledge"]["dataset_name"] == f"v72-faq-{tag}"
    prev = c.post(f"/voice/agents/{a['id']}/knowledge/search",
                  json={"query": "tell me the opening hours"})
    assert prev.status_code == 200 and prev.json()["matches"], prev.text
    sess = c.post("/voice/sessions", json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"CA-kb-{tag}",
        "from_ref": "+15550001", "to_ref": "+15550002", "agent_id": a["id"]})
    assert sess.status_code == 201, sess.text
    s = sess.json()
    assert s["agent"]["knowledge"]["dataset_id"] == ds["id"]
    ring = c.post(f"/voice/sessions/{s['id']}/events", json={"kind": "call.ringing"})
    assert ring.status_code == 200, ring.text
    ans = c.post(f"/voice/sessions/{s['id']}/events", json={"kind": "call.answered"})
    assert ans.status_code == 200 and ans.json()["greeting_tts"]["text"], ans.text
    c.post(f"/voice/sessions/{s['id']}/barge-in")
    turn = c.post(f"/voice/sessions/{s['id']}/turn",
                  json={"transcript": "What are your opening hours?"})
    assert turn.status_code == 200, turn.text
    t = turn.json()
    assert t["knowledge"] and t["reply"] == "We are open Monday to Friday from nine AM to six PM.", t
    c.post(f"/voice/sessions/{s['id']}/tts/complete")
    miss = c.post(f"/voice/sessions/{s['id']}/turn",
                  json={"transcript": "what is the moon made of"}).json()
    assert miss["reply"] == "no knowledge matched" and miss["knowledge"] is None, miss
    return {"reply": t["reply"], "agent_id": a["id"], "preview": prev.json()["matches"][0]}


def solution_check(c: httpx.Client, tag: str) -> dict:
    shelf = c.get("/solutions").json()
    mine = [s for s in shelf["solutions"] if s["slug"] == "voice-agent-system"]
    assert mine and mine[0]["voice_agent_ready"] is True, mine
    inst = c.post("/solutions/voice-agent-system/install", json={"as_voice_agent": True})
    assert inst.status_code == 200, inst.text
    va = inst.json()["voice_agent"]
    assert va and va["knowledge"]["dataset_name"].startswith("voice_agent_faq"), inst.text
    sess = c.post("/voice/sessions", json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"CA-sol-{tag}",
        "from_ref": "+15550009", "to_ref": "+15550010", "agent_id": va["id"]})
    assert sess.status_code == 201, sess.text
    s = sess.json()
    c.post(f"/voice/sessions/{s['id']}/events", json={"kind": "call.ringing"})
    ans = c.post(f"/voice/sessions/{s['id']}/events", json={"kind": "call.answered"})
    assert ans.json()["greeting_tts"]["text"].startswith("Hello, and thanks for calling")
    c.post(f"/voice/sessions/{s['id']}/barge-in")
    turn = c.post(f"/voice/sessions/{s['id']}/turn",
                  json={"transcript": "How long does shipping take?"})
    t = turn.json()
    assert t["reply"] == ("Standard shipping takes three to five business days. "
                          "Express shipping arrives the next business day."), t
    assert t["tts"]["provider"] == "openai_tts"
    return {"reply": t["reply"], "agent": va["name"]}


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="py8n-v72-smoke-")
    piper_bin, piper_voice = _make_stand_in_piper(tmp)
    db_path = f"{BACKEND}/data/smoke_v72_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PORT": "8199",
        "PY8N_PIPER_BIN": piper_bin,
        "PY8N_PIPER_VOICE": piper_voice,
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
            assert version == "1.72.0", version
            tag = uuid.uuid4().hex[:6]

            speech = speech_check(c, tag)
            print(f"[1] SPEECH ENGINES OK - the inventory is honest (vosk: "
                  f"'{speech['vosk_note'][:60]}...') and the REAL piper bridge bound at boot "
                  f"through PY8N_PIPER_BIN: /voice/tts/synthesize returned a RIFF wav "
                  f"({speech['duration']} ms); unregistered engines refuse loudly")

            kb = knowledge_check(c, tag)
            print(f"[2] KNOWLEDGE BINDING OK - the agent bound an FAQ dataset; the preview "
                  f"showed the grounding ('{kb['preview']['question']}') and a live call "
                  f"answered FROM the dataset: '{kb['reply']}'; an unmatched question got "
                  f"the honest fallback")

            sol = solution_check(c, tag)
            print(f"[3] VOICE AGENT SOLUTION OK - one-click install wired the FAQ dataset + "
                  f"knowledge handler + Voice Agent ('{sol['agent']}'); the phone answered "
                  f"from the installed knowledge: '{sol['reply']}'")

            print(f"\nALL 3 CHECKS GREEN - v72 live smoke passed (version {version})")
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
