"""V111 live smoke: boot the real server AND a real OpenAI-compatible LLM
bridge, then watch the HARNESS PATROL fire its own rounds over the actual
wire - the harness stops waiting to be asked.

1. THE SELF-RUN RHYTHM: a patrol (mission + session + a 5s rhythm) is
   enlisted and then NOBODY touches the API. The scheduler's sweep fires
   the rounds by itself: run_count climbs, every round is a REAL harness
   turn (patrol_start frame, find_instances over the real services, an
   answer from the estate). Pausing the patrol stops the rhythm.
2. THE GATE ON ITS OWN RHYTHM: a patrol whose mission walks the visitor -
   the round PAUSES at the approval gate (waiting_approval) with nobody
   watching; the instance stays in 'a'. The human approves over the API,
   the move happens AT DECISION TIME, and the outcome lands back on the
   patrol's receipt board.
3. THE RECEIPTS + HEALTH: the health door counts the patrols; the paused
   patrol's rhythm stays stopped.

Usage: python3 scripts/smoke_v111_patrol.py
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
# brain to walk the patrol rounds. Stateless per request - it decides by
# the conversation's markers (TOOL RESULTs) and the mission text.
app = FastAPI()


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    messages = body.get("messages", [])
    all_text = " ".join(m["content"] for m in messages)

    if "TOOL RESULT advance_instance" in all_text:
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
            {"answer": "Moved with the human's blessing: the visitor is in b."})}}]}
    if "walk the visitor forward" in all_text:
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
            {"tool": "advance_instance",
             "arguments": {"instance_id": STATE["instance_id"], "transition": "go"}})}}]}
    if "TOOL RESULT find_instances" in all_text:
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
            {"answer": "Patrol report: the desk is quiet - one visitor in state a."})}}]}
    if "patrol check-in" in all_text:
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
            {"tool": "find_instances",
             "arguments": {"process": "front desk smoke"}})}}]}
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(
        {"answer": "plain answer, no tools"})}}]}


STATE: dict = {}


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


def wait_for(fn, deadline: float, what: str):
    end = time.time() + deadline
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(0.5)
    raise SystemExit(f"timeout waiting for {what}: last={last}")


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v111_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",
        "PY8N_PATROL_TICK_SECONDS": "5",
        "PY8N_LLM_BRIDGE_URL": f"http://127.0.0.1:{BRIDGE_PORT}",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v111_server.log", "w")
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
            assert version == "1.111.0", version

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
            STATE["instance_id"] = r.json()["id"]

            # the harness session (memory none - every round stands alone)
            r = c.post("/harness/sessions", json={
                "name": "Smoke patrol ops", "memory": "none"})
            assert r.status_code == 201, r.text
            sess = r.json()

            # ---- 1) THE SELF-RUN RHYTHM: enlist and hands off
            r = c.post("/harness/patrols", json={
                "session_id": sess["id"], "name": "Night watch",
                "mission": "patrol check-in: who is on the front desk?",
                "interval_seconds": 5})
            assert r.status_code == 201, r.text
            patrol = r.json()
            assert patrol["run_count"] == 0

            def rounds_done():
                row = c.get(f"/harness/patrols/{patrol['id']}").json()
                return row if (row.get("run_count") or 0) >= 2 else None

            row = wait_for(rounds_done, 45, "the patrol to fire two rounds by itself")
            assert row["last_status"] == "completed", row
            r = c.get(f"/harness/patrols/{patrol['id']}/runs")
            runs = r.json()
            assert len(runs) >= 2, runs
            for t in runs:
                assert t["patrol_id"] == patrol["id"]
                assert t["trace"][0]["event"] == "patrol_start", t["trace"][0]
                assert t["tool_calls"][0]["tool"] == "find_instances"
                assert t["tool_calls"][0]["status"] == "ok"
                assert "state a" in t["reply"], t
            print(f"[1] SELF-RUN RHYTHM OK - with NOBODY touching the API the "
                  f"sweep fired {len(runs)} rounds on its own rhythm; every "
                  f"round was a REAL harness turn (patrol_start, find_instances "
                  f"over the real services) and answered from the estate: "
                  f"'{runs[0]['reply']}'")

            # pausing stops the rhythm
            r = c.patch(f"/harness/patrols/{patrol['id']}",
                        json={"is_active": False})
            assert r.json()["is_active"] is False
            time.sleep(7)  # one full tick + margin
            row = c.get(f"/harness/patrols/{patrol['id']}").json()
            frozen = row["run_count"]
            time.sleep(6)
            row = c.get(f"/harness/patrols/{patrol['id']}").json()
            assert row["run_count"] == frozen, (frozen, row["run_count"])
            print(f"[+] PAUSE OK - the paused patrol's rhythm stopped dead "
                  f"(run_count frozen at {frozen} across two ticks)")

            # ---- 2) THE GATE ON ITS OWN RHYTHM: the patrol asks, waits
            r = c.post("/harness/patrols", json={
                "session_id": sess["id"], "name": "Mover",
                "mission": "walk the visitor forward",
                "interval_seconds": 30})
            assert r.status_code == 201, r.text
            mover = r.json()

            def waiting():
                row = c.get(f"/harness/patrols/{mover['id']}").json()
                return row if row.get("last_status") == "waiting_approval" else None

            row = wait_for(waiting, 30, "the patrol round to pause at the gate")
            r = c.get(f"/processes/{proc_id}/instances/{STATE['instance_id']}")
            assert r.json()["state"] == "a", r.text  # nobody watched - nothing moved
            r = c.get("/harness/approvals?status=pending")
            slips = [s for s in r.json() if s["session_id"] == sess["id"]]
            assert len(slips) == 1 and slips[0]["tool"] == "advance_instance", slips

            # the human approves -> the move happens AT DECISION TIME and
            # the outcome lands on the patrol receipt
            r = c.post(f"/harness/approvals/{slips[0]['id']}/approve")
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "completed"
            r = c.get(f"/processes/{proc_id}/instances/{STATE['instance_id']}")
            assert r.json()["state"] == "b", r.text

            def receipt_done():
                row = c.get(f"/harness/patrols/{mover['id']}").json()
                return row if row.get("last_status") == "completed" else None

            row = wait_for(receipt_done, 10, "the patrol receipt to show completed")
            assert row["last_run_turn_id"] == slips[0]["turn_id"], row
            # retire the mover before its next rhythm fires another advance
            c.patch(f"/harness/patrols/{mover['id']}", json={"is_active": False})
            print(f"[2] THE GATE ON ITS OWN OK - the patrol round PAUSED at the "
                  f"gate with nobody watching (the instance stayed in 'a'); the "
                  f"human approved over the API, the move executed at decision "
                  f"time ('b') and the receipt board shows the completed round")

            # ---- 3) receipts + health
            r = c.get("/harness/health")
            body = r.json()
            assert body["patrols"] == 2 and body["active_patrols"] == 0, body
            assert body["guard"]["patrol_tick_seconds"] == 5, body
            print(f"[3] RECEIPTS + HEALTH OK - health counts the patrols "
                  f"(2 total, 0 active after the smoke paused both) and the "
                  f"guard config carries the patrol tick")
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
