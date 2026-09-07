"""V95 live smoke: boot the real server and walk the cross-machine view.

1. THE CHAIN MAP: Operations + Sales + Finance install; GET
   /processes/chains derives the owner-wide Revenue chain (Lead pipeline
   --won--> Customer onboarding --handed_off--> Invoice lifecycle) from
   what is INSTALLED, with live node counts. A fresh lead WINS and the
   leg's ride counts + HISTORY name the traversal - the same wire the
   operator-detail chain overlays its history from.
2. THE CROSS-MACHINE ESCALATION HEATMAP: a machine with a tiny SLA goes
   past it on the real clock; the door tick knocks; the handler acks;
   GET /processes/escalation-history reads today's cell - the escalation
   AND the ack in one grid cell, per machine, off the log.

Usage: /home/z/.venv/bin/python scripts/smoke_v95_live.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v74_server.py"
API = "http://127.0.0.1:8218/api/v1"
SERVER_PORT = 8218


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


def chain_map_check(c: httpx.Client) -> dict:
    for slug in ("operations-operator", "sales-operator", "finance-operator"):
        r = c.post(f"/operators/{slug}/install", json={})
        assert r.status_code == 200, r.text

    r = c.get("/processes/chains")
    out = r.json()
    names = [ch["name"] for ch in out["chains"]]
    assert "Revenue chain" in names, names
    revenue = next(ch for ch in out["chains"] if ch["name"] == "Revenue chain")
    assert revenue["head_name"] == "Lead pipeline", revenue
    legs = revenue["legs"]
    assert [(l["from_name"], l["on_state"], l["to_name"]) for l in legs] == [
        ("Lead pipeline", "won", "Customer onboarding"),
        ("Customer onboarding", "handed_off", "Invoice lifecycle")], legs
    assert legs[1]["due_in_seconds"] == 5 * 24 * 3600, legs
    assert out["nodes"], out

    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    lead_pid = procs["Lead pipeline"]
    r = c.post(f"/processes/{lead_pid}/instances",
               json={"ref": "+15557770999", "title": "Smoke Chain Deal"})
    iid = r.json()["id"]
    for move in ("reach_out", "qualify", "book_demo", "run_demo",
                 "send_proposal", "negotiate", "win"):
        r = c.post(f"/processes/{lead_pid}/instances/{iid}/advance",
                   json={"transition": move})
        assert r.status_code == 200, r.text

    r = c.get("/processes/chains")
    revenue = next(ch for ch in r.json()["chains"] if ch["name"] == "Revenue chain")
    leg1 = revenue["legs"][0]
    assert leg1["opened"] == 1 and leg1["open_now"] == 1, leg1
    hist = leg1["history"]
    assert hist and hist[0]["ref"] == "+15557770999", hist
    assert hist[0]["state"] == "kickoff" and hist[0]["opened_at"], hist
    return {"leg_history_refs": [h["ref"] for h in hist]}


def heatmap_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={
        "name": "Smoke heat machine",
        "definition": {"states": ["a", "b", "done"], "initial": "a",
                       "transitions": [{"name": "go", "from": "a", "to": "b"},
                                       {"name": "finish", "from": "b", "to": "done"}],
                       "escalation_policy": {
                           "channel": "email", "to": "ops@py8n.test",
                           "repeat_every_seconds": 60, "max_repeats": 3}}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "HEAT-95", "title": "the hot one",
                     "due_in_seconds": 1})
    assert r.status_code == 201, r.text
    iid = r.json()["id"]

    # the SLA passes on the real clock, the door knocks
    time.sleep(2.3)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    assert report["scanned"] >= 1, report

    # the human's receipt lands in the same cell
    r = c.post(f"/processes/{pid}/instances/{iid}/escalations/ack",
               json={"by": "smoke-handler", "note": "on it"})
    assert r.status_code == 200, r.text

    r = c.get("/processes/escalation-history?days=14")
    out = r.json()
    assert len(out["days"]) == 14 and out["days"][-1] == time.strftime("%Y-%m-%d"), out
    machines = {m["name"]: m for m in out["machines"]}
    assert "Smoke heat machine" in machines, machines
    cell = machines["Smoke heat machine"]["cells"][time.strftime("%Y-%m-%d")]
    assert cell["escalations"] >= 1, cell
    assert cell["acks"] >= 1, cell
    return {"today": cell}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v95_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the door
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
            assert version == "1.95.0", version

            chain_map_check(c)
            print(f"[1] CHAIN MAP OK - Operations + Sales + Finance installed; "
                  f"GET /processes/chains derived the owner-wide Revenue chain "
                  f"from what is installed (the leg's own 5-day SLA riding it); "
                  f"the fresh lead WON and the leg's ride counts moved (opened "
                  f"1, moving 1) with the traversal in the leg's HISTORY - "
                  f"the exact wire the operator-detail chain overlays")

            heatmap_check(c)
            print(f"[2] CROSS-MACHINE ESCALATION HEATMAP OK - the heat machine "
                  f"went past its SLA on the real clock; the door tick knocked; "
                  f"the handler acked; GET /processes/escalation-history read "
                  f"the 14-day grid off the log with today's cell holding BOTH "
                  f"the escalation and the ack - per machine, per day, "
                  f"derived, nothing stored twice")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
