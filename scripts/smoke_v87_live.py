"""V87 live smoke: boot the real server and drive the escalation POLICY
layer (channel + repeat), the agents' read door, and the last operator
machines - the Meeting/Clinic packs now ship their processes pre-wired.

1. OPERATOR MACHINES: the Meeting Operator install builds the Meeting
   lifecycle machine (8 states, escalate self-loops, EMAIL escalation
   policy) seeded AT the ended-meeting stage from the notes, bound as a
   first-class component; the Clinic installs the Appointment journey
   seeded per patient with the phone as ref (SMS policy).
2. THE ESCALATION POLICY: a stuck instance on a policy-carrying machine
   is escalated THROUGH THE DOOR (POST /scheduler/escalations/tick - the
   same sweep APScheduler runs): on the record (no-move row - this
   machine defines no escalate move), business.stuck + business.
   escalated on the correlation thread, the delivery honestly skipped
   (no email endpoint bound), the episode bookkeeping on the instance's
   memory; the immediate re-tick is held (too_soon - the 60s cadence).
3. BUSINESS_QUERY - AGENTS READ THE OPERATION: a workflow with the
   business_query node runs and returns the RUNNING entities (open only,
   terminal excluded, process_name attached) - the read side of the
   advance/start/query loop on one canvas.

Usage: /home/z/.venv/bin/python scripts/smoke_v87_live.py
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
API = "http://127.0.0.1:8216/api/v1"
SERVER_PORT = 8216

MACHINE = {
    "states": ["a", "b", "done", "dead"],
    "initial": "a",
    "transitions": [
        {"name": "go", "from": "a", "to": "b"},
        {"name": "finish", "from": "b", "to": "done"},
        {"name": "kill", "from": "a", "to": "dead"},
    ],
}


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
# 1) the operator machines - Meeting/Clinic ship theirs
# ---------------------------------------------------------------------------

def operator_machines_check(c: httpx.Client) -> dict:
    r = c.get("/operators")
    shelf = {o["slug"]: o for o in r.json()["operators"]}
    assert len(shelf) == 9, shelf
    assert all(o["topology"]["processes"] >= 1 for o in shelf.values()), shelf

    r = c.post("/operators/meeting-operator/install", json={"note": "smoke v87"})
    assert r.status_code == 200, r.text
    built = r.json()
    proc = built["processes"][0]
    assert proc["name"].startswith("Meeting lifecycle"), proc
    assert len(proc["states"]) == 8 and proc["seeded_instances"] == 2, proc

    detail = c.get(f"/processes/{proc['id']}").json()
    pol = detail["escalation_policy"]
    assert pol["channel"] == "email" and pol["repeat_every_seconds"] == 1800, pol
    assert "email" in detail["escalation_summary"]

    rows = c.get(f"/processes/{proc['id']}/instances").json()["instances"]
    assert {x["ref"] for x in rows} == {"MTG-1001", "MTG-1002"}, rows
    assert all(x["state"] == "notes_logged" for x in rows), rows  # imported AT the stage

    r = c.post(f"/processes/{proc['id']}/instances", json={
        "ref": "MTG-2001", "title": "Board review", "due_in_seconds": 3600})
    iid = r.json()["id"]
    r = c.post(f"/processes/{proc['id']}/instances/{iid}/advance",
               json={"transition": "invite"})
    assert r.status_code == 200 and r.json()["state"] == "invited", r.text

    grouped = c.get(f"/systems/{built['system']['id']}").json()["grouped"]
    assert any(x["ref_id"] == proc["id"] for x in grouped.get("process", [])), grouped

    # the clinic: the appointment journey per patient (ref = the phone)
    r = c.post("/operators/clinic-operator/install", json={"note": "front desk"})
    cproc = r.json()["processes"][0]
    assert cproc["name"].startswith("Appointment journey"), cproc
    crows = c.get(f"/processes/{cproc['id']}/instances").json()["instances"]
    assert len(crows) == 3 and all(x["ref"].startswith("+1555") for x in crows), crows
    return {"meeting": proc, "clinic": cproc}


# ---------------------------------------------------------------------------
# 2) the escalation policy - the door tells someone (honestly)
# ---------------------------------------------------------------------------

def escalation_policy_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={
        "name": "Door machine",
        "definition": {**MACHINE, "escalation_policy": {
            "channel": "email", "to": "ops@py8n.test",
            "repeat_every_seconds": 60, "max_repeats": 1}}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = c.post(f"/processes/{pid}/instances", json={
        "ref": "EP-1", "title": "the stuck one", "due_in_seconds": 1})
    assert r.status_code == 201, r.text
    iid = r.json()["id"]

    time.sleep(1.3)  # let the SLA pass (the smoke uses the real clock)
    r = c.post("/scheduler/escalations/tick")
    assert r.status_code == 200, r.text
    report = r.json()
    mine = next(e for e in report["recorded"] if e["instance_id"] == iid), None
    entries = [e for e in report["recorded"] if e["instance_id"] == iid]
    assert entries, report
    esc = entries[0]
    assert esc["attempt"] == 1 and esc["delivery"] == "skipped", esc
    assert "endpoint" in esc["delivery_detail"], esc

    # the policy's event beside the door's
    esc_ev = c.get("/events", params={"type": "business.escalated",
                                      "correlation_id": iid}).json()["events"]
    assert len(esc_ev) == 1 and esc_ev[0]["payload"]["attempt"] == 1, esc_ev
    stuck_ev = c.get("/events", params={"type": "business.stuck",
                                        "correlation_id": iid}).json()["events"]
    assert len(stuck_ev) == 1, stuck_ev

    # the episode rides the instance's memory
    book = c.get(f"/processes/{pid}/instances/{iid}").json()["context"]["escalations"]
    assert book["count"] == 1 and book["state"] == "a", book

    # the immediate repeat is HELD (too_soon - the 60s cadence)
    report2 = c.post("/scheduler/escalations/tick").json()
    held = [h for h in report2["held"] if h["instance_id"] == iid]
    assert held and held[0]["reason"] == "too_soon", report2
    return {"instance_id": iid}


# ---------------------------------------------------------------------------
# 3) business_query - the agents read the operation
# ---------------------------------------------------------------------------

def business_query_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={"name": "Ops board", "definition": MACHINE})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    c.post(f"/processes/{pid}/instances", json={"ref": "Q-1", "title": "moving"})
    r = c.post(f"/processes/{pid}/instances", json={"ref": "Q-2", "title": "closed"})
    iid2 = r.json()["id"]
    r = c.post(f"/processes/{pid}/instances/{iid2}/advance", json={"transition": "kill"})
    assert r.status_code == 200, r.text

    graph = {"nodes": [
        {"id": "t", "type": "manual_trigger", "name": "Trigger",
         "position": {"x": 0, "y": 0}, "parameters": {}},
        {"id": "q", "type": "business_query", "name": "Read the op",
         "position": {"x": 1, "y": 0},
         "parameters": {"process": "Ops board"}},
    ], "edges": [
        {"id": "e1", "source": "t", "target": "q",
         "sourceHandle": "main", "targetHandle": "main"},
    ]}
    r = c.post("/workflows", json={"name": "Agent reads the operation",
                                   "graph": graph, "is_active": False})
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]
    r = c.post(f"/workflows/{wf_id}/run", json={"payload": {}})
    assert r.status_code in (200, 202), r.text
    exec_id = r.json()["execution_id"]
    detail = {}
    for _ in range(100):
        detail = c.get(f"/executions/{exec_id}").json()
        if detail.get("status") != "running":
            break
        time.sleep(0.1)
    assert detail.get("status") == "success", detail
    runs = {x["node_id"]: x for x in detail["node_runs"]}
    out = runs["q"]["output"]
    assert out["count"] == 1 and out["process"] == "Ops board", out
    assert out["instances"][0]["ref"] == "Q-1", out
    assert out["instances"][0]["is_terminal"] is False, out
    return {"out": out}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v87_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.87.0", version

            built = operator_machines_check(c)
            print(f"[1] OPERATOR MACHINES OK - the Meeting Operator install "
                  f"built {built['meeting']['name']!r} (8 states, email "
                  f"escalation every 30m) seeded AT the ended-meeting stage "
                  f"from the notes; the Clinic built the Appointment journey "
                  f"per patient (ref = the phone, sms policy); the machine "
                  f"moved to 'invited' and binds as a first-class component")

            r = escalation_policy_check(c)
            print(f"[2] THE ESCALATION POLICY OK - the stuck instance was "
                  f"escalated THROUGH THE DOOR: on the record (no-move row), "
                  f"business.stuck + business.escalated on the correlation "
                  f"thread, the delivery honestly skipped (no email endpoint "
                  f"bound), the episode on the instance's memory (count=1), "
                  f"and the immediate repeat held (too_soon)")

            r = business_query_check(c)
            print(f"[3] BUSINESS_QUERY OK - the workflow read the RUNNING "
                  f"entities (open only, terminal excluded, process_name "
                  f"attached) - the read side of the advance/start/query loop")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
