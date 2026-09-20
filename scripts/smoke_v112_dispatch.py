"""V112 live smoke: boot the real server, a real OpenAI-compatible LLM
bridge AND a real SMTP sink, then watch a patrol's findings WALK OUT of
py8n over the actual wire - the dispatch, end to end.

1. THE ROUND WRITES HOME: a patrol with dispatch_to finishes a real
   round (a real harness turn through the real services) and the SINK
   RECEIVES the mail - subject "[py8n patrol] ... - completed", the
   round's answer in the body, the receipt board stamped ok.
2. THE GATE ON ITS OWN RHYTHM: a patrol whose mission walks the visitor
   pauses at the approval gate - and NOTHING mails (a pause is not a
   finding). The human approves over the API, the move happens AT
   DECISION TIME, and the outcome lands in the sink carrying what the
   decision made real.
3. THE HANDSHAKE: the dispatch door before any round sends the handshake
   mail; a patrol without recipients is a loud 400.

Usage: python3 scripts/smoke_v112_dispatch.py
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
SINK = "/home/z/my-project/py8n/scripts/dev_smtp_sink.py"
PY = ("/home/z/.venv/bin/python"
      if os.path.exists("/home/z/.venv/bin/python") else "python3")
sys.path.insert(0, "/home/z/my-project/py8n/scripts")
from dev_smtp_sink import SmtpDevSink  # noqa: E402


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
# brain to walk the patrol rounds and answer from the estate.
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
    db_path = f"{BACKEND}/data/smoke_v112_{uuid.uuid4().hex[:8]}.sqlite3"
    sink = SmtpDevSink(port=_free_port()).start()

    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",
        "PY8N_PATROL_TICK_SECONDS": "3600",  # the smoke fires rounds by hand
        "PY8N_LLM_BRIDGE_URL": f"http://127.0.0.1:{BRIDGE_PORT}",
        "PY8N_SMTP_HOST": "127.0.0.1",
        "PY8N_SMTP_PORT": str(sink.port),
        "PY8N_SMTP_USE_TLS": "false",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v112_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER], cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    bridge_cfg = uvicorn.Config(app, host="127.0.0.1", port=BRIDGE_PORT, log_level="error")
    bridge_server = uvicorn.Server(bridge_cfg)
    threading.Thread(target=bridge_server.run, daemon=True).start()
    time.sleep(1.0)
    checks: list[tuple[str, bool]] = []
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.114.0", version

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
                "name": "Smoke dispatch ops", "memory": "none"})
            assert r.status_code == 201, r.text
            sess = r.json()

            # ---- 1) THE ROUND WRITES HOME
            r = c.post("/harness/patrols", json={
                "session_id": sess["id"], "name": "Night watch",
                "mission": "patrol check-in: who is on the front desk?",
                "interval_seconds": 3600,
                "dispatch_to": "chief@py8n.smoke, ops@py8n.smoke"})
            assert r.status_code == 201, r.text
            assert r.json()["dispatch_to"] == "chief@py8n.smoke, ops@py8n.smoke"
            patrol = r.json()

            r = c.post(f"/harness/patrols/{patrol['id']}/run")
            assert r.status_code == 200 and r.json()["status"] == "completed", r.text

            def sink_has(n):
                return sink.messages if sink.count >= n else None

            mail = wait_for(lambda: sink_has(1), 30, "the round's mail to reach the sink")
            body = mail[0]["text"]
            assert "Patrol report: the desk is quiet" in body, body
            assert "Mission: patrol check-in" in body, body
            assert "Night watch" in mail[0]["subject"], mail[0]["subject"]
            assert "chief@py8n.smoke" in mail[0]["to"], mail[0]
            checks.append(("THE ROUND WRITES HOME (real SMTP round trip)", True))

            r = c.get(f"/harness/patrols/{patrol['id']}")
            row = r.json()
            assert row["last_dispatch_status"] == "ok", row
            assert "2 recipient(s)" in row["last_dispatch_detail"], row
            checks.append(("THE RECEIPT BOARD STAMPS THE DISPATCH", True))

            # ---- 2) THE GATE ON ITS OWN RHYTHM: silence until the decision
            r = c.post("/harness/patrols", json={
                "session_id": sess["id"], "name": "Gate walker",
                "mission": "walk the visitor forward",
                "interval_seconds": 3600,
                "dispatch_to": "chief@py8n.smoke"})
            assert r.status_code == 201, r.text
            gated = r.json()

            r = c.post(f"/harness/patrols/{gated['id']}/run")
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "waiting_approval", r.text
            before = sink.count

            # nothing moved, nothing mailed - a pause is not a finding
            r = c.get(f"/processes/{proc_id}/instances/{STATE['instance_id']}")
            assert r.json()["state"] == "a", r.text
            assert sink.count == before, sink.count

            r = c.get("/harness/approvals?status=pending")
            slips = r.json()
            assert len(slips) == 1, r.text

            r = c.post(f"/harness/approvals/{slips[0]['id']}/approve")
            assert r.status_code == 200 and r.json()["status"] == "completed", r.text

            mail2 = wait_for(lambda: sink_has(before + 1), 30,
                             "the decision's mail to reach the sink")
            body2 = mail2[-1]["text"]
            assert "Moved with the human's blessing" in body2, body2
            checks.append(("THE GATE HOLDS THE MAIL - THE DECISION SENDS IT", True))

            r = c.get(f"/processes/{proc_id}/instances/{STATE['instance_id']}")
            assert r.json()["state"] == "b", r.text
            r = c.get(f"/harness/patrols/{gated['id']}")
            assert r.json()["last_status"] == "completed", r.json()
            assert r.json()["last_dispatch_status"] == "ok", r.json()

            # ---- 3) THE HANDSHAKE + the quiet patrol
            r = c.post("/harness/patrols", json={
                "session_id": sess["id"], "name": "Handshake only",
                "mission": "prove the walk", "interval_seconds": 3600,
                "dispatch_to": "chief@py8n.smoke"})
            fresh = r.json()
            before = sink.count
            r = c.post(f"/harness/patrols/{fresh['id']}/dispatch")
            assert r.status_code == 200 and r.json()["last_dispatch_status"] == "ok", r.text
            mail3 = wait_for(lambda: sink_has(before + 1), 30, "the handshake mail")
            assert "handshake" in mail3[-1]["subject"], mail3[-1]["subject"]
            checks.append(("THE HANDSHAKE DOOR PROVES THE WALK", True))

            quiet = c.post("/harness/patrols", json={
                "session_id": sess["id"], "name": "Quiet",
                "mission": "no recipients", "interval_seconds": 3600}).json()
            r = c.post(f"/harness/patrols/{quiet['id']}/dispatch")
            assert r.status_code == 400, r.text
            checks.append(("NO RECIPIENTS IS A LOUD 400", True))

        print()
        print("=" * 64)
        ok = all(passed for _, passed in checks)
        for name, passed in checks:
            print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        print("=" * 64)
        print(f"v112 dispatch smoke: {len(checks)} checks - "
              f"{'ALL GREEN' if ok else 'FAILURES ABOVE'}")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        sink.stop()


if __name__ == "__main__":
    raise SystemExit(main())
