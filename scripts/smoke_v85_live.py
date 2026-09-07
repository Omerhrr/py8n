"""V85 live smoke: boot the real server and drive OPERATORS INSTALLING
PRE-WIRED PROCESSES + THE ESCALATION DOOR end to end.

1. THE SALES OPERATOR SHIPS THE LEAD PIPELINE BOUND: install composes a
   real BusinessProcess (escalate self-loops included), seeds one
   instance per CRM lead AT ITS CRM STAGE (ref = the phone), binds it
   kind=process into the RUNNING system, and the Pipeline advancer
   workflow's business_advance node points at the BUILT process id.
2. THE PIPELINE MOVES ITSELF ON A REAL CALL: boot the system (the boot
   door activates the reactive workflows), emit a REAL call.ended event
   whose actor is a tracked lead's phone -> the advancer moves the lead
   lead -> contacted (a real execution); a call from a lead already past
   the move skips honestly (the run stays green, the state unmoved); and
   the v83 scorer still writes its Lead events row - both workflows react.
3. THE SCHEDULER DOOR: a machine that defines its own 'escalate'
   self-loop gets walked through it when the SLA breaches (business.stuck
   + business.state_changed both land, the stint re-arms), a machine
   without one gets the escalation RECORDED, and the door knocks once
   per stint (the second tick is honest).

Usage: /home/z/.venv/bin/python scripts/smoke_v85_live.py
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
API = "http://127.0.0.1:8215/api/v1"
SERVER_PORT = 8215


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


def _drain() -> None:
    time.sleep(1.5)


# ---------------------------------------------------------------------------
# 1) the operator ships the lead pipeline bound
# ---------------------------------------------------------------------------

def install_check(c: httpx.Client) -> dict:
    r = c.get("/operators/sales-operator")
    assert r.status_code == 200, r.text
    procs = r.json()["installs"]["processes"]
    assert len(procs) == 1 and procs[0]["name"] == "Lead pipeline", procs
    assert procs[0]["escalates"] is True and procs[0]["seeded_from"] == "CRM leads", procs

    r = c.post("/operators/sales-operator/install", json={})
    assert r.status_code == 200, r.text
    built = r.json()
    assert built["system"]["lifecycle"] == "running", built["system"]
    assert built["system"]["components"].get("process") == 1, built["system"]
    proc = built["processes"][0]
    assert proc["seeded_instances"] == 3, proc
    assert built["system"]["components"].get("workflow") >= 3  # scorer + advancer + handler

    # seeded AT the CRM's stage
    r = c.get(f"/processes/{proc['id']}/instances")
    insts = r.json()["instances"]
    stages = {i["ref"]: i["state"] for i in insts}
    assert stages["+15550001333"] == "lead", stages      # Mia Silva
    assert stages["+15550001111"] == "contacted", stages  # Dana Reyes (imported)
    assert stages["+15550001222"] == "interested", stages  # Ari Cohen (imported)

    # the advancer's node binds the BUILT process id
    advancer = next(w for w in built["workflows"] if w["name"] == "Pipeline advancer")
    r = c.get(f"/workflows/{advancer['id']}")
    node = next(n for n in r.json()["graph"]["nodes"] if n["type"] == "business_advance")
    assert node["parameters"]["process"] == proc["id"], node["parameters"]
    assert node["parameters"]["ref"] == "{{ input.actor }}", node["parameters"]
    return {"built": built, "proc": proc, "advancer": advancer}


# ---------------------------------------------------------------------------
# 2) the pipeline moves itself on a real call
# ---------------------------------------------------------------------------

def reaction_check(c: httpx.Client, built: dict, proc: dict, advancer: dict) -> dict:
    sid = built["system"]["id"]
    r = c.post(f"/systems/{sid}/stop")
    assert r.status_code == 200, r.text
    r = c.post(f"/systems/{sid}/start", json={"activate_workflows": True})
    assert r.status_code == 200, r.text
    assert r.json()["workflows_activated"] >= 3, r.text  # scorer + advancer + handler

    # a REAL call.ended whose actor is the tracked lead's phone
    mia_ref = "+15550001333"
    r = c.post("/events", json={
        "type": "call.ended", "source": "voice", "actor": mia_ref,
        "payload": {"end_reason": "completed", "state": "ended"},
        "correlation_id": "smoke-call-mia", "session_id": "smoke-call-mia"})
    assert r.status_code == 201, r.text
    _drain()

    r = c.get(f"/processes/{proc['id']}/instances", params={"state": "contacted"})
    refs = [i["ref"] for i in r.json()["instances"]]
    assert mia_ref in refs, refs  # the pipeline moved ITSELF: lead -> contacted

    r = c.get("/events", params={"type": "business.state_changed",
                                 "correlation_id": ""})
    # find Mia's instance id via the pipeline, then read its thread
    r = c.get(f"/processes/{proc['id']}/instances")
    mia = next(i for i in r.json()["instances"] if i["ref"] == mia_ref)
    r = c.get("/events", params={"type": "business.state_changed",
                                 "correlation_id": mia["id"]})
    evs = r.json()["events"]
    assert len(evs) == 1 and evs[0]["payload"]["to"] == "contacted", evs

    # a call from a lead ALREADY past the move: skips honestly, run green
    r = c.post("/events", json={
        "type": "call.ended", "source": "voice", "actor": "+15550001111",
        "payload": {"end_reason": "completed", "state": "ended"},
        "correlation_id": "smoke-call-dana", "session_id": "smoke-call-dana"})
    assert r.status_code == 201, r.text
    _drain()
    runs = c.get("/executions", params={"limit": 50}).json()
    adv_runs = [x for x in runs if x.get("workflow_id") == advancer["id"]]
    assert len(adv_runs) == 2 and all(x["status"] == "success" for x in adv_runs), adv_runs
    r = c.get(f"/processes/{proc['id']}/instances")
    dana = next(i for i in r.json()["instances"] if i["ref"] == "+15550001111")
    assert dana["state"] == "contacted"  # unmoved - the call is evidence, not a move

    # the v83 scorer still reacts beside the advancer
    runs = c.get("/executions", params={"limit": 50}).json()
    scorer = next(w for w in built["workflows"] if w["name"] == "Lead scorer")
    scorer_runs = [x for x in runs if x.get("workflow_id") == scorer["id"]]
    assert len(scorer_runs) == 2 and all(x["status"] == "success" for x in scorer_runs), scorer_runs
    return {"mia": mia["id"]}


# ---------------------------------------------------------------------------
# 3) the scheduler door
# ---------------------------------------------------------------------------

def door_check(c: httpx.Client) -> None:
    # machine WITH the escalate self-loop
    r = c.post("/processes", json={
        "name": "Escalatable deals", "definition": {
            "states": ["interested", "won", "lost"],
            "initial": "interested",
            "transitions": [
                {"name": "win", "from": "interested", "to": "won"},
                {"name": "lose", "from": "interested", "to": "lost"},
                {"name": "escalate", "from": "interested", "to": "interested"},
            ]}})
    assert r.status_code == 201, r.text
    pid1 = r.json()["id"]
    r = c.post(f"/processes/{pid1}/instances", json={
        "ref": "DEAL-88", "due_in_seconds": 1})
    iid1 = r.json()["id"]

    # machine WITHOUT one
    r = c.post("/processes", json={
        "name": "Plain cases", "definition": {
            "states": ["open", "review", "closed"],
            "initial": "open",
            "transitions": [
                {"name": "review", "from": "open", "to": "review"},
                {"name": "close", "from": "review", "to": "closed"},
            ]}})
    assert r.status_code == 201, r.text
    pid2 = r.json()["id"]
    r = c.post(f"/processes/{pid2}/instances", json={
        "ref": "CASE-88", "due_in_seconds": 1})
    iid2 = r.json()["id"]

    time.sleep(1.6)  # the SLA breaches, deterministically

    r = c.post("/scheduler/escalations/tick")
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["escalated"]) == 1 and out["escalated"][0]["moved_to"] == "interested", out
    assert len(out["recorded"]) == 1 and out["recorded"][0]["instance_id"] == iid2, out

    # BOTH facts on the escalated instance's thread + the stint re-armed
    r = c.get("/events", params={"correlation_id": iid1})
    types = {e["type"] for e in r.json()["events"]}
    assert {"business.stuck", "business.state_changed"} <= types, types
    r = c.get(f"/processes/{pid1}/instances/{iid1}")
    assert r.json()["age_in_state_seconds"] < 5, r.json()

    # the recorded (no-move) escalation is on the record
    r = c.get(f"/processes/{pid2}/instances/{iid2}")
    journey = r.json()["journey"]
    assert any(j["transition"] == "escalated" for j in journey), journey
    assert r.json()["state"] == "open"

    # one knock per stint - the second sweep holds its fire
    r = c.post("/scheduler/escalations/tick")
    out = r.json()
    assert not out["escalated"] and not out["recorded"], out
    assert out["already"] >= 2, out


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v85_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.85.0", version

            got = install_check(c)
            print(f"[1] THE OPERATOR SHIPS THE LEAD PIPELINE BOUND OK - the sales "
                  f"operator composed the Lead pipeline machine (escalate self-loops "
                  f"on the stalemable states), seeded 3 instances AT their CRM stages "
                  f"(ref = the phone), bound it kind=process into the RUNNING system, "
                  f"and the advancer's business_advance node points at the BUILT "
                  f"process id (system {got['built']['system']['id'][:8]})")

            reaction_check(c, got["built"], got["proc"], got["advancer"])
            print(f"[2] THE PIPELINE MOVES ITSELF ON A REAL CALL OK - boot opened the "
                  f"reactive path; a REAL call.ended (actor = the tracked phone) moved "
                  f"the lead lead -> contacted (business.state_changed on the "
                  f"instance's thread), a call from an already-contacted lead skipped "
                  f"honestly (both advancer runs green, the state unmoved), and the "
                  f"v83 scorer still scored beside it")

            door_check(c)
            print(f"[3] THE SCHEDULER DOOR OK - the machine WITH an escalate move got "
                  f"walked through it (both business.stuck AND business.state_changed "
                  f"landed, the stint re-armed), the machine WITHOUT one got the "
                  f"escalation RECORDED on the journey, and the second tick knocked "
                  f"nothing (one escalation per stint, honestly counted)")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
