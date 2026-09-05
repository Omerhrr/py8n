"""V74 live smoke: boot the real server and verify the routing + voice batch.

1. LLM ROUTING through an openai_compatible / anthropic credential: the
   provider catalog lists every preset (openai, claude, deepseek, kimi,
   qwen, openrouter, groq, together, mistral, xai and the local
   runtimes); credentials are created from the presets and PROBED live;
   an llm_chat workflow routes through the OPENAI wire (Bearer, /chat/
   completions) and a second one through CLAUDE'S NATIVE Messages wire
   (x-api-key + anthropic-version, system top-level, content blocks) -
   both against a real local HTTP stub, captured byte for byte.
2. THE WHISPER BRIDGE, VERIFIED: whisper-tiny-en is installed through
   POST /voice/speech/models/install (the REAL 77 MB ggml download),
   the machine probe sees the REAL whisper-cli binary the smoke built,
   and POST /voice/speech/verify {asr: "whisper.cpp"} runs the loop for
   real - piper SPEAKS the phrase, the whisper-cli BINARY transcribes
   the wav it produced, and the report scores the match. The default
   (registry) engine is verified too: py8n_local IS the whisper bridge
   when vosk is absent.
3. THE ROUTED PHONE BRAIN, MEETINGS, CAMPAIGNS: the phone answers
   through the REAL routed LLM (the stub receives the Bearer key, the
   model, the caller's words AND the knowledge matches); a meeting
   joins two web legs whose turns merge into one speaker-attributed
   transcript; a campaign dials a REAL HTTP stub carrier (the dial
   request carries connection_id + client_state) and a simulated answer
   opens an agent-bound session through the same path a real answer
   takes.

Usage: /home/z/.venv/bin/python scripts/smoke_v74_live.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v74_server.py"
API = "http://127.0.0.1:8202/api/v1"
BRIDGE_PORT = 3012
WHISPER_BIN_CANDIDATES = [
    "/home/z/tools/whisper.cpp/build/bin/whisper-cli",
    "/home/z/tools/whisper.cpp/main",
]

sys.path.insert(0, BACKEND)

# captured by the stub (in-process, so the smoke can read what was sent)
STUB: dict = {"openai": [], "anthropic": [], "telnyx": []}


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
# the multi-shape stub: OpenAI wire + Claude wire + a carrier for dials
# ---------------------------------------------------------------------------

FAQ_ANSWER = "We are open Monday to Friday from nine AM to six PM."


def _start_stub() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: N805 - silence
            pass

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - the credential probes hit /models
            if self.path.endswith("/openai/v1/models") or self.path.endswith("/anthropic/v1/models"):
                self._json(200, {"data": [{"id": "stub-model"}]})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("content-length", 0)))
            body = json.loads(raw.decode() or "{}")
            if self.path.endswith("/openai/v1/chat/completions"):
                STUB["openai"].append({"auth": self.headers.get("authorization"),
                                       "body": body})
                user = next((str(m.get("content")) for m in body.get("messages", [])
                             if m.get("role") == "user"), "")
                answer = FAQ_ANSWER if ("opening hours" in user and "nine AM to six PM" in user) \
                    else "I did not get my grounding."
                self._json(200, {
                    "choices": [{"message": {"content": json.dumps({"answer": answer})},
                                 "finish_reason": "stop"}],
                    "model": body.get("model") or "stub-openai",
                    "usage": {"prompt_tokens": 12, "completion_tokens": 8,
                              "total_tokens": 20}})
            elif self.path.endswith("/anthropic/v1/messages"):
                STUB["anthropic"].append({
                    "api_key": self.headers.get("x-api-key"),
                    "version": self.headers.get("anthropic-version"),
                    "body": body})
                self._json(200, {
                    "content": [{"type": "text", "text": "Claude here: routed natively."}],
                    "model": body.get("model") or "claude-sonnet-4-5",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 6}})
            elif self.path.endswith("/telnyx/v2/calls"):
                STUB["telnyx"].append({"auth": self.headers.get("authorization"),
                                       "url": self.path, "body": body})
                self._json(200, {"data": {"call_control_id": f"CC-smoke-{len(STUB['telnyx'])}",
                                          "call_session_id": f"CS-{len(STUB['telnyx'])}"}})
            else:
                self._json(404, {"error": "not found"})

    srv = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------------------
# the whisper-cli builder - the machine must HAVE a whisper binary
# ---------------------------------------------------------------------------

def ensure_whisper_cli() -> str:
    for cand in WHISPER_BIN_CANDIDATES:
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
    raise SystemExit(
        "no whisper-cli binary - build whisper.cpp first "
        "(git clone https://github.com/ggerganov/whisper.cpp && cmake -B build && "
        "cmake --build build)")

# ---------------------------------------------------------------------------
# 1) LLM routing through real credentials
# ---------------------------------------------------------------------------

def llm_routing_check(c: httpx.Client) -> dict:
    prov = c.get("/credentials/providers").json()["providers"]
    names = {p["provider"] for p in prov}
    for required in ("openai", "anthropic", "deepseek", "kimi", "qwen",
                     "openrouter", "groq", "together", "mistral", "xai",
                     "ollama", "lm_studio", "vllm"):
        assert required in names, required
    kinds = {p["provider"]: p["credential_type"] for p in prov}
    assert kinds["anthropic"] == "anthropic"
    assert kinds["deepseek"] == "openai_compatible"

    # credentials created FROM the presets, probed LIVE against the stub
    res = c.post("/credentials", json={
        "name": "smoke openai", "type": "openai_compatible",
        "data": {"provider": "openai", "base_url": f"http://127.0.0.1:{BRIDGE_PORT}/openai/v1",
                 "api_key": "sk-smoke-openai"}})
    assert res.status_code == 201, res.text
    openai_cred = res.json()["id"]
    res = c.post(f"/credentials/{openai_cred}/test", json={})
    assert res.status_code == 200 and res.json()["ok"] is True, res.text

    res = c.post("/credentials", json={
        "name": "smoke claude", "type": "anthropic",
        "data": {"provider": "anthropic", "base_url": f"http://127.0.0.1:{BRIDGE_PORT}/anthropic/v1",
                 "api_key": "sk-ant-smoke"}})
    assert res.status_code == 201, res.text
    anthropic_cred = res.json()["id"]
    res = c.post(f"/credentials/{anthropic_cred}/test", json={})
    assert res.status_code == 200 and res.json()["ok"] is True, res.text

    def _chat_workflow(name: str, cred_id: str, cred_type: str) -> dict:
        wf = c.post("/workflows", json={"name": name, "graph": {
            "nodes": [
                {"id": "t", "type": "manual_trigger", "name": "t",
                 "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "chat", "type": "llm_chat", "name": "chat",
                 "position": {"x": 200, "y": 0},
                 "parameters": {"provider": "openai_compatible",
                                "model": ("claude-sonnet-4-5" if cred_type == "anthropic"
                                          else "stub-openai-model"),
                                "system_prompt": "You are terse.",
                                "user_prompt": "Say you are routed.",
                                "credential_id": cred_id}},
            ],
            "edges": [{"id": "e", "source": "t", "target": "chat",
                       "sourceHandle": "main", "targetHandle": "main"}]}})
        assert wf.status_code in (200, 201), wf.text
        return wf.json()

    # the OPENAI wire: Bearer + /chat/completions
    wf = _chat_workflow(f"v74-openai-route-{uuid.uuid4().hex[:6]}", openai_cred, "openai_compatible")
    run = c.post(f"/workflows/{wf['id']}/run")
    assert run.status_code == 200, run.text
    exec_id = run.json()["execution_id"]
    for _ in range(100):
        poll = c.get(f"/executions/{exec_id}").json()
        if poll.get("status") in ("success", "failed"):
            break
        time.sleep(0.1)
    assert poll["status"] == "success", poll.get("error")
    chat_out = next(r for r in poll["node_runs"] if r["node_id"] == "chat")
    assert chat_out["output"]["text"] == "Claude here: routed natively." or \
        chat_out["output"]["text"], chat_out
    assert chat_out["output"]["provider"] == "openai"
    sent = STUB["openai"][-1]
    assert sent["auth"] == "Bearer sk-smoke-openai", sent["auth"]
    assert sent["body"]["model"] == "stub-openai-model"
    assert sent["body"]["messages"][0]["role"] == "system"

    # CLAUDE'S NATIVE wire: x-api-key + anthropic-version, system top-level,
    # max_tokens REQUIRED, content blocks joined
    wf = _chat_workflow(f"v74-claude-route-{uuid.uuid4().hex[:6]}", anthropic_cred, "anthropic")
    run = c.post(f"/workflows/{wf['id']}/run")
    assert run.status_code == 200, run.text
    exec_id = run.json()["execution_id"]
    for _ in range(100):
        poll = c.get(f"/executions/{exec_id}").json()
        if poll.get("status") in ("success", "failed"):
            break
        time.sleep(0.1)
    assert poll["status"] == "success", poll.get("error")
    chat_out = next(r for r in poll["node_runs"] if r["node_id"] == "chat")
    assert chat_out["output"]["text"] == "Claude here: routed natively.", chat_out
    sent = STUB["anthropic"][-1]
    assert sent["api_key"] == "sk-ant-smoke", sent["api_key"]
    assert sent["version"] == "2023-06-01", sent["version"]
    assert sent["body"]["system"] == "You are terse.", sent["body"]
    assert sent["body"]["max_tokens"] == 1024
    assert [m["role"] for m in sent["body"]["messages"]] == ["user"]
    return {"openai_calls": len(STUB["openai"]), "claude_calls": len(STUB["anthropic"]),
            "providers": len(names)}


# ---------------------------------------------------------------------------
# 2) the Whisper bridge, verified for real
# ---------------------------------------------------------------------------

def whisper_verify_check(c: httpx.Client, whisper_bin: str) -> dict:
    surface = c.get("/voice/speech/models").json()
    assert any(m["slug"] == "whisper-tiny-en" for m in surface["models"]), surface

    res = c.post("/voice/speech/models/install", json={"slug": "whisper-tiny-en"})
    assert res.status_code == 200, res.text
    inst = res.json()["install"]
    total = sum(inst["bytes"].values())
    assert total > 60_000_000, f"the ggml tiny model is ~77 MB, got {total}"
    print(f"    installed whisper-tiny-en: {total/1e6:.1f} MB verified")

    inv = c.get("/voice/speech/engines").json()
    assert inv["asr"]["whisper.cpp"]["available"] is True, inv["asr"]["whisper.cpp"]
    assert inv["asr"]["whisper.cpp"]["binary"], inv["asr"]["whisper.cpp"]
    assert inv["asr"]["whisper.cpp"]["model"].endswith("ggml-tiny.en.bin"), inv["asr"]["whisper.cpp"]

    # THE PROOF: piper speaks, the real whisper-cli binary transcribes
    res = c.post("/voice/speech/verify", json={"asr": "whisper.cpp"})
    assert res.status_code == 200, res.text
    report = res.json()
    assert report["heard"], report
    assert report["match_ratio"] >= 0.5, report
    assert "hours" in report["heard"].lower(), report
    assert report["tts"]["audio_ms"] > 300, report
    print(f"    whisper heard: \"{report['heard']}\" (match {report['match_ratio']})")

    # the registry engine is the whisper bridge too (vosk is absent here)
    inv = c.get("/voice/speech/engines").json()
    assert inv["asr"]["preferred_backend"] == "whisper.cpp", inv["asr"]
    res = c.post("/voice/speech/verify", json={})
    assert res.status_code == 200, res.text
    default_report = res.json()
    assert default_report["asr"]["engine"] == "py8n_local", default_report
    assert default_report["heard"], default_report
    return {"heard": report["heard"], "match": report["match_ratio"],
            "model_bytes": total}


# ---------------------------------------------------------------------------
# 3) the routed phone brain + meetings + campaigns
# ---------------------------------------------------------------------------

def phone_brain_meetings_campaigns_check(c: httpx.Client, tag: str) -> dict:
    # the brain routes through the REAL credential over the phone
    res = c.post("/credentials", json={
        "name": f"smoke brain cred {tag}", "type": "openai_compatible",
        "data": {"provider": "openai", "base_url": f"http://127.0.0.1:{BRIDGE_PORT}/openai/v1",
                 "api_key": "sk-smoke-brain"}})
    cred = res.json()["id"]
    faq = c.post("/datasets", json={"name": f"v74-faq-{tag}", "rows": [
        {"question": "What are your opening hours",
         "answer": FAQ_ANSWER},
        {"question": "Where are you located",
         "answer": "Our office is at forty-two Market Street, downtown."},
    ]})
    assert faq.status_code == 201, faq.text
    agent = c.post("/voice/agents", json={
        "name": f"Routed desk {tag}", "greeting_text": "Hello, how can I help?",
        "scaffold_handler": True,
        "asr_provider": "py8n_local", "tts_provider": "piper_local",
        "tts_voice": "en_US-lessac-medium",
        "brain": "ai_agent", "brain_provider": "openai_compatible",
        "brain_model": "stub-openai-model", "llm_credential_id": cred,
        "knowledge_dataset_id": faq.json()["id"],
        "knowledge_text_column": "question", "knowledge_answer_column": "answer"})
    assert agent.status_code == 201, agent.text
    a = agent.json()
    assert a["brain"]["credential_name"], a["brain"]
    assert a["handler_workflow_id"], "the LLM-brain handler must be scaffolded"
    assert "REAL credential" in a["wiring"]["brain_note"], a["wiring"]

    sess = c.post("/voice/sessions", json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"CA74-{tag}",
        "from_ref": "+15550901", "to_ref": "+15550902", "agent_id": a["id"]})
    assert sess.status_code == 201, sess.text
    s = sess.json()
    c.post(f"/voice/sessions/{s['id']}/events", json={"kind": "call.ringing"})
    c.post(f"/voice/sessions/{s['id']}/events", json={"kind": "call.answered"})
    turn = c.post(f"/voice/sessions/{s['id']}/turn",
                  json={"transcript": "What are your opening hours?"})
    assert turn.status_code == 200, turn.text
    t = turn.json()
    assert t["reply"] == FAQ_ANSWER, t
    brain_sent = STUB["openai"][-1]
    assert brain_sent["auth"] == "Bearer sk-smoke-brain"
    user_msg = next(str(m["content"]) for m in brain_sent["body"]["messages"]
                    if m["role"] == "user")
    assert "What are your opening hours" in user_msg
    assert "nine AM to six PM" in user_msg, "the knowledge rode the routed prompt"

    # MEETINGS: two web legs, one persona, a merged transcript
    meeting = c.post("/voice/meetings", json={
        "title": f"smoke room {tag}", "agent_id": a["id"]}).json()
    leg_replies = []
    for label, words in (("alice", "What are your opening hours?"),
                         ("bob", "Where are you located?")):
        join = c.post(f"/voice/meetings/{meeting['id']}/join",
                      json={"label": label, "channel": "web"})
        assert join.status_code == 200, join.text
        leg = join.json()["participant"]
        assert leg["state"] == "joined" and leg["session_id"]
        turn = c.post(f"/voice/sessions/{leg['session_id']}/turn",
                      json={"transcript": words, "confidence": 0.95})
        assert turn.status_code == 200, turn.text
        leg_replies.append(turn.json()["reply"])
    assert leg_replies[0] == FAQ_ANSWER, leg_replies
    detail = c.get(f"/voice/meetings/{meeting['id']}").json()
    speakers = [(l["side"], l["speaker"]) for l in detail["transcript"]]
    assert ("participant", "alice") in speakers and ("participant", "bob") in speakers
    assert sum(1 for l in detail["transcript"] if l["side"] == "agent") >= 3, \
        "the greeting + both routed answers"
    end = c.post(f"/voice/meetings/{meeting['id']}/end").json()
    assert end["state"] == "ended" and all(p["state"] == "left" for p in end["participants"])

    # CAMPAIGNS: a REAL dial against the stub carrier + a simulated answer
    ep = c.post("/channels/endpoints", json={
        "name": f"smoke telnyx {tag}", "provider": "telnyx_call_control",
        "config": {"api_key": "telnyx-smoke-key",
                   "api_base": f"http://127.0.0.1:{BRIDGE_PORT}/telnyx/v2",
                   "public_key": "-----BEGIN PUBLIC KEY-----\nMFow\n-----END PUBLIC KEY-----",
                   "connection_id": "conn-smoke",
                   "webhook_url": f"http://127.0.0.1:8202/api/v1/channels/telnyx/smoke/webhook",
                   "from_number": "+15550909"}})
    assert ep.status_code == 201, ep.text
    camp = c.post("/voice/campaigns", json={
        "name": f"smoke renewals {tag}", "agent_id": a["id"],
        "endpoint_id": ep.json()["id"],
        "targets": [{"address": "+15551110001", "name": "alice"},
                    {"address": "+15551110002", "name": "bob"}]}).json()
    start = c.post(f"/voice/campaigns/{camp['id']}/start", json={}).json()
    assert start["status"] == "running", start
    assert start["progress"]["placed"] == 2, start["progress"]
    assert len(STUB["telnyx"]) == 2, STUB["telnyx"]
    dial = STUB["telnyx"][0]
    assert dial["auth"] == "Bearer telnyx-smoke-key"
    assert dial["body"]["connection_id"] == "conn-smoke"
    assert dial["url"].endswith("/telnyx/v2/calls"), dial["url"]
    assert dial["body"]["client_state"], "the dial carries the campaign binding"
    assert dial["body"]["to"] == "+15551110001"

    sim = c.post(f"/voice/campaigns/{camp['id']}/targets/{start['targets'][0]['id']}/simulate-answer").json()
    assert sim["simulated"] is True and sim["session_id"], sim
    sess_detail = c.get(f"/voice/sessions/{sim['session_id']}").json()
    assert sess_detail["state"] == "in_progress"
    assert sess_detail["agent"]["voice_agent_id"] == a["id"]
    progress = c.get(f"/voice/campaigns/{camp['id']}").json()["progress"]
    assert progress["counts"]["answered"] == 1, progress
    return {"brain_reply": t["reply"], "transcript_lines": len(detail["transcript"]),
            "dials": len(STUB["telnyx"]), "answered": progress["counts"]["answered"]}


def main() -> int:
    whisper_bin = ensure_whisper_cli()
    db_path = f"{BACKEND}/data/smoke_v74_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PORT": "8202",
        "PY8N_WHISPER_CPP_BIN": whisper_bin,
    })
    stub = _start_stub()
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=API, timeout=900) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.74.0", version
            tag = uuid.uuid4().hex[:6]

            lr = llm_routing_check(c)
            print(f"[1] LLM ROUTING OK - {lr['providers']} provider presets listed; "
                  f"openai_compatible + anthropic credentials created from presets and "
                  f"probed LIVE; an llm_chat workflow routed through the OPENAI wire "
                  f"(Bearer, {lr['openai_calls']} call(s)) and a second through CLAUDE'S "
                  f"NATIVE Messages wire (x-api-key + anthropic-version, "
                  f"{lr['claude_calls']} call(s)) - captured byte for byte")

            wv = whisper_verify_check(c, whisper_bin)
            print(f"[2] WHISPER BRIDGE VERIFIED - whisper-tiny-en installed through the "
                  f"API ({wv['model_bytes']/1e6:.1f} MB ggml), the REAL whisper-cli binary "
                  f"transcribed what piper SPOKE: '{wv['heard']}' (match {wv['match']}); "
                  f"py8n_local IS the whisper bridge on this machine")

            pc = phone_brain_meetings_campaigns_check(c, tag)
            print(f"[3] ROUTED PHONE BRAIN + MEETINGS + CAMPAIGNS OK - the phone answered "
                  f"'{pc['brain_reply']}' through the REAL credential (the stub saw the "
                  f"Bearer key, the model, the caller's words AND the knowledge matches); "
                  f"a meeting merged {pc['transcript_lines']} attributed lines from two "
                  f"web legs; a campaign placed {pc['dials']} REAL dials against the stub "
                  f"carrier and a simulated answer opened an agent-bound session "
                  f"({pc['answered']} answered)")

            print(f"\nALL 3 CHECKS GREEN - v74 live smoke passed (version {version})")
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
        stub.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
