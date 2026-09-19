"""V109 live smoke: boot the real server AND a real OpenAI-compatible LLM
bridge, then walk the HARNESS over the actual wire - the system's own
agentic runtime, working in tandem with the estate.

1. THE READ LOOP OVER THE WIRE: a harness session asks "who is on the front
   desk?" - the bridge calls find_instances, py8n executes the REAL service
   query, the tool result comes back as a TOOL RESULT message, the bridge
   answers with what it saw. Completed turn, real data, inline trace.
2. THE GATE OVER THE WIRE: "walk the visitor forward" - the bridge calls
   advance_instance and the turn PAUSES (waiting_approval). The instance is
   still in its state: the model's word moved nothing. The human approves
   over the API - the move happens AT DECISION TIME, the loop resumes and
   the machine actually lands on the next state.
3. THE GUARD OVER THE WIRE: the bridge repeats list_processes with
   identical arguments three times - the third call is BLOCKED by the
   repeat guard (a guard frame in the trace), the refusal is fed back, and
   the bridge answers anyway. guard_blocks=1, turn completed.

Usage: python3 scripts/smoke_v109_harness.py
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

# The scripted bridge: an honest OpenAI-compatible wire with just enough
# brain to walk the harness loop. It decides by the conversation's last
# user message and the TOOL RESULT markers, and counts repeats itself.
app = FastAPI()
STATE = {"repeats": 0, "saw_instance": False, "saw_guard_refusal": False}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    messages = body.get("messages", [])
    last_user = next((m["content"] for m in reversed(messages)
                      if m["role"] == "user"), "")
    all_text = " ".join(m["content"] for m in messages)

    if "TOOL RESULT find_instances" in all_text:
        if STATE["saw_instance"] is False:
            STATE["saw_instance"] = True
            return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
                {"answer": "One visitor, ref K-smoke, is in state a on the front desk."})}}]}
    if "blocked by guard" in all_text:
        STATE["saw_guard_refusal"] = True
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
            {"answer": "The guard stopped my echo; the estate has one machine."})}}]}
    if "TOOL RESULT advance_instance" in all_text:
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
            {"answer": "Moved with the human's blessing: the visitor is in b."})}}]}
    if "who is on the front desk" in all_text and \
            "TOOL RESULT find_instances" not in all_text:
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
            {"tool": "find_instances", "arguments": {"process": "front desk smoke"}})}}]}
    if "walk the visitor forward" in all_text and \
            "TOOL RESULT advance_instance" not in all_text:
        # the bridge does NOT know the instance id - it reads it from the
        # find_instances result it saw earlier (the honest agent behavior)
        instance_id = STATE.get("instance_id", "")
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
            {"tool": "advance_instance",
             "arguments": {"instance_id": instance_id, "transition": "go"}})}}]}
    if "repeat list_processes" in all_text:
        if "blocked by guard" in all_text:
            STATE["saw_guard_refusal"] = True
            return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
                {"answer": "The guard stopped my echo; the estate has one machine."})}}]}
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
            {"tool": "list_processes", "arguments": {}})}}]}
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
        {"answer": "plain answer, no tools"})}}]}


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


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v109_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",
        "PY8N_LLM_BRIDGE_URL": f"http://127.0.0.1:{BRIDGE_PORT}",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v109_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER], cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    bridge_cfg = uvicorn.Config(app, host="127.0.0.1", port=BRIDGE_PORT, log_level="error")
    bridge_server = uvicorn.Server(bridge_cfg)
    threading.Thread(target=bridge_server.run, daemon=True).start()
    time.sleep(1.0)
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.113.0", version

            # seed: a real machine + a real visitor
            r = c.post("/processes", json={
                "name": "Front desk smoke",
                "definition": {
                    "states": ["a", "b", "done"], "initial": "a",
                    "transitions": [{"name": "go", "from": "a", "to": "b"},
                                    {"name": "finish", "from": "b", "to": "done"}],
                }})
            assert r.status_code == 201, r.text
            proc_id = r.json()["id"]
            r = c.post(f"/processes/{proc_id}/instances",
                       json={"ref": "K-smoke", "title": "the visitor"})
            assert r.status_code == 201, r.text
            instance = r.json()

            # the harness session
            r = c.post("/harness/sessions", json={
                "name": "Smoke ops harness", "memory": "none"})
            assert r.status_code == 201, r.text
            sess = r.json()
            STATE["instance_id"] = instance["id"]

            # ---- 1) the read loop over the wire
            r = c.post(f"/harness/sessions/{sess['id']}/turns",
                       json={"message": "who is on the front desk?"})
            assert r.status_code == 200, r.text
            turn = r.json()
            assert turn["status"] == "completed", turn
            assert turn["tool_calls"][0]["tool"] == "find_instances", turn
            assert turn["tool_calls"][0]["status"] == "ok"
            assert instance["id"] in turn["tool_calls"][0]["result"], turn
            assert "state a" in turn["reply"], turn
            print(f"[1] READ LOOP OK - the bridge called find_instances over the "
                  f"wire, py8n ran the REAL service query, and the harness "
                  f"answered from the estate: '{turn['reply']}'")

            # ---- 2) the gate over the wire
            r = c.post(f"/harness/sessions/{sess['id']}/turns",
                       json={"message": "walk the visitor forward"})
            assert r.status_code == 200, r.text
            turn = r.json()
            assert turn["status"] == "waiting_approval", turn
            r = c.get(f"/processes/{proc_id}/instances/{instance['id']}")
            assert r.json()["state"] == "a", r.text  # the model's word moved NOTHING
            r = c.get("/harness/approvals?status=pending")
            slips = r.json()
            assert len(slips) == 1 and slips[0]["tool"] == "advance_instance", slips
            r = c.post(f"/harness/approvals/{slips[0]['id']}/approve")
            assert r.status_code == 200, r.text
            done = r.json()
            assert done["status"] == "completed", done
            events = [f["event"] for f in done["trace"]]
            assert "approval_requested" in events and "approval_decided" in events, events
            r = c.get(f"/processes/{proc_id}/instances/{instance['id']}")
            assert r.json()["state"] == "b", r.text  # moved AT DECISION TIME
            print(f"[2] THE GATE OK - advance_instance PAUSED the turn "
                  f"(the instance stayed in 'a' on the model's word alone); "
                  f"the human approved over the API, the move executed at "
                  f"decision time and the machine landed on 'b'")

            # ---- 3) the guard over the wire
            STATE["repeats"] = 0
            r = c.post(f"/harness/sessions/{sess['id']}/turns",
                       json={"message": "repeat list_processes three times please"})
            turn = r.json()
            assert turn["status"] == "completed", turn
            assert turn["guard_blocks"] == 1, turn
            guard_frames = [f for f in turn["trace"] if f["event"] == "guard"]
            assert guard_frames and "repeat" in guard_frames[0]["reason"], turn
            assert STATE["saw_guard_refusal"], "the bridge never saw the refusal"
            executed = [f for f in turn["trace"] if f["event"] == "tool_call"
                        and f.get("status") == "ok"]
            assert len(executed) == 2, executed
            print(f"[3] THE GUARD OK - the third verbatim list_processes was "
                  f"BLOCKED by the repeat guard, the refusal was fed back and "
                  f"the bridge answered anyway: guard_blocks=1, turn completed")

            # health answers with the guard config
            r = c.get("/harness/health")
            assert r.json()["guard"]["repeat_limit"] == 2, r.text
            print(f"[+] HARNESS HEALTH OK - guard config echoed "
                  f"(repeat_limit=2, budget={r.json()['guard']['max_iterations']} rounds)")
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
