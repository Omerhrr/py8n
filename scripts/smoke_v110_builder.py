"""V110 live smoke: boot the real server AND a real OpenAI-compatible LLM
bridge, then walk the HARNESS BUILDER over the actual wire - the harness
stops only operating the estate and BUILDS on it.

1. THE BLUEPRINT OVER THE WIRE: "draft me a lead desk" - the bridge calls
   draft_machine, py8n runs the REAL composer's synthesizer + validator, and
   the bridge reads the validated spec off the TOOL RESULT (the honest agent
   move). Completed turn - and the estate provably has NO new dataset,
   workflow or system: a blueprint is free.
2. THE BUILD GATE OVER THE WIRE: "build it" - the bridge relays the EXACT
   spec it saw into build_machine and the turn PAUSES (waiting_approval).
   The estate is still empty: the model's word builds nothing. The human
   approves over the API - the composer's REAL build path runs at decision
   time and the machine lands: the Leads dataset, the (inactive) Lead
   intake workflow, the running system binding them.
3. THE OPERATOR GATE OVER THE WIRE: "install the sales operator" - the
   bridge calls install_operator and the turn PAUSES. The human REJECTS -
   the refusal is fed back over the wire, the bridge answers gracefully,
   and the estate provably gained nothing (no processes, no second system).

Usage: python3 scripts/smoke_v110_builder.py
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
# brain to walk the builder loop. It decides by the conversation's markers
# and keeps the blueprint in STATE like a real agent keeps its context.
app = FastAPI()
STATE = {"spec": None, "saw_refusal": False}


def _tool(name: str, arguments: dict) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
        {"tool": name, "arguments": arguments})}}]}


def _answer(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
        {"answer": text})}}]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    messages = body.get("messages", [])
    all_text = " ".join(m.get("content", "") for m in messages)
    last_user = next((m["content"] for m in reversed(messages)
                      if m["role"] == "user"), "")

    if "TOOL RESULT build_machine" in all_text:
        return _answer("The lead desk is built on your word: the dataset, "
                       "the intake workflow and the running system.")
    if "DECLINED by the human operator" in all_text:
        STATE["saw_refusal"] = True
        return _answer("Understood - the install was declined; the estate "
                       "stands exactly as it was.")
    if "TOOL RESULT draft_machine" in all_text:
        # keep the blueprint the way a real agent would - read off the wire
        try:
            payload = json.loads(last_user.split(": ", 1)[1])
            STATE["spec"] = payload.get("spec")
        except Exception:
            pass
        return _answer("The blueprint is drafted: dataset, intake workflow, "
                       "agent, room and queue - nothing built yet.")
    if "draft me a lead desk" in all_text:
        return _tool("draft_machine", {"description": "a lead desk for my sales team"})
    if "build it" in all_text and STATE.get("spec"):
        return _tool("build_machine", {"spec": STATE["spec"]})
    if "install the sales operator" in all_text:
        return _tool("install_operator", {"slug": "sales-operator",
                                          "brain": "scaffold"})
    return _answer("plain answer, no tools")


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
    db_path = f"{BACKEND}/data/smoke_v110_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",
        "PY8N_LLM_BRIDGE_URL": f"http://127.0.0.1:{BRIDGE_PORT}",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v110_server.log", "w")
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

            # the harness session (fresh memory each turn - the bridge is the brain)
            r = c.post("/harness/sessions", json={
                "name": "Smoke builder harness", "memory": "none"})
            assert r.status_code == 201, r.text
            sess = r.json()

            # ---- 1) the blueprint over the wire - and the nothing-built proof
            r = c.post(f"/harness/sessions/{sess['id']}/turns",
                       json={"message": "draft me a lead desk"})
            assert r.status_code == 200, r.text
            turn = r.json()
            assert turn["status"] == "completed", turn
            assert turn["tool_calls"][0]["tool"] == "draft_machine", turn
            assert turn["tool_calls"][0]["status"] == "ok"
            assert "blueprint only" in turn["tool_calls"][0]["result"], turn
            assert STATE["spec"], "the bridge never received the validated spec"
            assert STATE["spec"]["name"], STATE["spec"]
            r = c.get("/datasets")
            assert r.json() == [], r.text          # a blueprint is FREE
            r = c.get("/systems")
            assert r.json() == [], r.text
            print(f"[1] THE BLUEPRINT OK - draft_machine ran the REAL composer "
                  f"(archetype={STATE['spec'].get('archetype', '?')}), the bridge "
                  f"read the validated spec off the wire, and the estate has "
                  f"NO new dataset, workflow or system")

            # ---- 2) the build gate over the wire
            r = c.post(f"/harness/sessions/{sess['id']}/turns",
                       json={"message": "build it"})
            assert r.status_code == 200, r.text
            turn = r.json()
            assert turn["status"] == "waiting_approval", turn
            r = c.get("/datasets")
            assert r.json() == [], r.text          # the model's word builds NOTHING
            r = c.get("/harness/approvals?status=pending")
            slips = r.json()
            assert len(slips) == 1 and slips[0]["tool"] == "build_machine", slips
            spec_on_slip = slips[0]["arguments"]["spec"]
            assert spec_on_slip.get("components"), slips  # the WHOLE spec is on the slip
            r = c.post(f"/harness/approvals/{slips[0]['id']}/approve")
            assert r.status_code == 200, r.text
            done = r.json()
            assert done["status"] == "completed", done
            decided = next(f for f in done["trace"] if f["event"] == "approval_decided")
            assert decided["decision"] == "approved" and decided["status"] == "ok", decided
            assert decided.get("decided_by") is None  # trust-the-wire install: no principal
            r = c.get("/datasets")
            datasets = r.json()
            assert any(d["name"] == "Leads" for d in datasets), r.text
            r = c.get("/workflows")
            intake = next((w for w in r.json() if w["name"] == "Lead intake"), None)
            assert intake is not None and intake["is_active"] is False, r.text
            r = c.get("/systems")
            systems = r.json()
            assert len(systems) == 1 and systems[0]["lifecycle"] == "running", r.text
            print(f"[2] THE BUILD GATE OK - build_machine PAUSED the turn "
                  f"(the estate stayed empty on the model's word alone); the "
                  f"human approved over the API, the composer's REAL build ran "
                  f"at decision time: Leads dataset + inactive Lead intake "
                  f"workflow + a RUNNING system")

            # ---- 3) the operator gate over the wire - a NO keeps the estate
            r = c.post(f"/harness/sessions/{sess['id']}/turns",
                       json={"message": "install the sales operator"})
            assert r.status_code == 200, r.text
            turn = r.json()
            assert turn["status"] == "waiting_approval", turn
            r = c.get("/harness/approvals?status=pending")
            slip = r.json()[0]
            assert slip["tool"] == "install_operator", slip
            r = c.post(f"/harness/approvals/{slip['id']}/reject")
            assert r.status_code == 200, r.text
            done = r.json()
            assert done["status"] == "completed", done
            assert STATE["saw_refusal"], "the bridge never saw the refusal"
            r = c.get("/processes")
            assert r.json()["processes"] == [], r.text   # no operator business
            r = c.get("/systems")
            assert len(r.json()) == 1, r.text            # only the built machine
            r = c.get("/harness/approvals?status=pending")
            assert r.json() == []
            print(f"[3] THE OPERATOR GATE OK - install_operator PAUSED the "
                  f"turn; the human REJECTED over the API, the refusal crossed "
                  f"the wire, the bridge answered gracefully and the estate "
                  f"provably gained nothing")

            # health answers with the guard config
            r = c.get("/harness/health")
            assert r.json()["ok"] is True, r.text
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
