"""V108 live smoke: boot the real server AND a real OpenAI-compatible LLM
bridge, then walk the agent-module round over the actual wire - the agent
part as a module, working.

1. THE MODULE ROUND OVER THE WIRE: a module is created (system prompt +
   knowledge tool + buffer memory) and run through POST /agents/modules/{id}/run
   against the REAL bridge - the model calls the handbook tool, the tool
   result comes back as a TOOL RESULT message, the model answers. Reply,
   iterations, tool_calls and the inline trace land in the response.
2. SESSION MEMORY OVER THE WIRE: the second turn on the same session key
   carries the first turn's history (the bridge sees the assistant turn and
   says so), memory_turns_loaded names it, and the sessions door lists the
   conversation - then forgets it on DELETE.
3. THE CODE TOOL IS REAL: the bridge drives the calc code tool (result =
   21 * 2) and the sandbox executes it - the model's answer quotes 42,
   computed by py8n, not by the model.

Usage: python3 scripts/smoke_v108_live.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid

import httpx
import uvicorn
from fastapi import FastAPI, Request

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v74_server.py"
PY = ("/home/z/.venv/bin/python"
      if os.path.exists("/home/z/.venv/bin/python") else "python3")


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


SERVER_PORT = _free_port()
BRIDGE_PORT = _free_port()
API = f"http://127.0.0.1:{SERVER_PORT}/api/v1"

# The scripted bridge: an honest OpenAI-compatible wire that decides what
# to say by reading the conversation's LAST user message - exactly enough
# brain to walk the module loop, no more.
app = FastAPI()
SEE_HISTORY: dict = {"saw_history": False}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    messages = body.get("messages", [])
    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    has_history = any(m["role"] == "assistant" for m in messages)
    if has_history:
        SEE_HISTORY["saw_history"] = True
    all_text = " ".join(m["content"] for m in messages)
    if "TOOL RESULT handbook" in all_text:
        content = json.dumps({"answer": "Rule one: the human picks the moment."})
    elif "what is rule one" in last_user:
        content = json.dumps({"tool": "handbook", "arguments": {}})
    elif "do you remember" in last_user:
        content = json.dumps({"answer": "I remember the handbook turn."})
    elif "TOOL RESULT calc" in all_text:
        content = json.dumps({"answer": "42"})
    elif "what is twenty-one times two" in last_user:
        content = json.dumps({"tool": "calc", "arguments": {"code": "result = 21 * 2"}})
    else:
        content = json.dumps({"answer": "plain answer, no tools"})
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def wait_health(client: httpx.Client, deadline: float = 240.0) -> None:
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
# 1) the module round over the wire
# ---------------------------------------------------------------------------

def module_round_check(c: httpx.Client) -> dict:
    r = c.post("/agents/modules", json={
        "name": "Smoke Ops Assistant",
        "description": "v108 smoke",
        "system_prompt": "You are precise.",
        "memory": "buffer",
        "tools": [{"kind": "knowledge", "name": "handbook", "description": "the handbook",
                   "content": "Py8n ops rule one: the human picks the moment."}],
    })
    assert r.status_code == 201, r.text
    mod = r.json()
    assert mod["tool_kinds"] == ["knowledge"], mod

    r = c.post(f"/agents/modules/{mod['id']}/run",
               json={"message": "what is rule one?", "session_key": "play"})
    assert r.status_code == 200, r.text
    turn1 = r.json()
    assert turn1["reply"] == "Rule one: the human picks the moment.", turn1
    assert turn1["iterations"] == 2, turn1
    assert turn1["tool_calls"][0]["tool"] == "handbook", turn1
    assert turn1["tool_calls"][0]["status"] == "ok", turn1
    assert turn1["memory_turns_loaded"] == 0, turn1
    assert turn1["trace"][-1]["event"] == "answer", turn1
    return {"module_id": mod["id"], "reply": turn1["reply"]}


# ---------------------------------------------------------------------------
# 2) session memory over the wire
# ---------------------------------------------------------------------------

def memory_check(c: httpx.Client, module_id: str) -> dict:
    r = c.post(f"/agents/modules/{module_id}/run",
               json={"message": "do you remember?", "session_key": "play"})
    assert r.status_code == 200, r.text
    turn = r.json()
    assert turn["reply"] == "I remember the handbook turn.", turn
    assert turn["memory_turns_loaded"] == 1, turn
    assert SEE_HISTORY["saw_history"], "the bridge never saw the stored history"

    r = c.get(f"/agents/modules/{module_id}/sessions")
    assert r.status_code == 200, r.text
    sessions = r.json()
    assert [s["session_key"] for s in sessions] == ["play"], sessions
    assert sessions[0]["turns"] == 2, sessions

    r = c.delete(f"/agents/modules/{module_id}/sessions/play")
    assert r.json()["cleared"] is True, r.text
    r = c.get(f"/agents/modules/{module_id}/sessions")
    assert r.json() == [], r.text
    return {"remembered": True}


# ---------------------------------------------------------------------------
# 3) the code tool is real
# ---------------------------------------------------------------------------

def code_tool_check(c: httpx.Client) -> dict:
    r = c.post("/agents/modules", json={
        "name": "Smoke Calculator",
        "memory": "none",
        "tools": [{"kind": "code", "name": "calc", "description": "arithmetic",
                   "timeout_seconds": 5}],
    })
    assert r.status_code == 201, r.text
    mod = r.json()
    r = c.post(f"/agents/modules/{mod['id']}/run",
               json={"message": "what is twenty-one times two?"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["reply"] == "42", out
    assert out["tool_calls"][0]["status"] == "ok", out
    assert '"result": 42' in out["tool_calls"][0]["result"], out
    # the old inventory door still answers beside the modules
    r = c.get("/agents")
    assert r.status_code == 200, r.text
    return {"computed": out["reply"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v108_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",
        "PY8N_LLM_BRIDGE_URL": f"http://127.0.0.1:{BRIDGE_PORT}",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v108_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER], cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    # the bridge lives in THIS process - the backend talks to it over TCP
    bridge_cfg = uvicorn.Config(app, host="127.0.0.1", port=BRIDGE_PORT, log_level="error")
    bridge_server = uvicorn.Server(bridge_cfg)
    threading.Thread(target=bridge_server.run, daemon=True).start()
    time.sleep(1.0)
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.114.0", version

            first = module_round_check(c)
            print(f"[1] MODULE ROUND OK - the module answered "
                  f"'{first['reply']}' after a REAL tool loop over the wire: "
                  f"the bridge called the handbook tool, py8n executed it, "
                  f"the answer came back with the inline trace")

            memory_check(c, first["module_id"])
            print(f"[2] SESSION MEMORY OK - turn two rode turn one's stored "
                  f"history (the bridge SAW the assistant turn), "
                  f"memory_turns_loaded named it, the sessions door listed "
                  f"'play' with both turns and then honestly forgot it")

            code_tool_check(c)
            print(f"[3] CODE TOOL OK - the bridge drove the calc tool and the "
                  f"REAL sandbox computed 21*2=42 (the model quoted py8n's "
                  f"work, not its own); GET /agents still answers beside the "
                  f"modules")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        bridge_server.should_exit = True


if __name__ == "__main__":
    sys.exit(main())
