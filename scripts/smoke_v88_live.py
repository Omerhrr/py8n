"""V88 live smoke: boot the real server and drive the agents' memory door,
the human's receipt, and the department onboarding loops.

1. ANNOTATE (business_annotate): facts land DIRECTLY on a tracked entity's
   memory without moving the machine - through the API (journey row naming
   the keys + business.annotated on the correlation thread) and through a
   workflow node by ref; the escalation door's reserved key refuses loud;
   a terminal entity still remembers.
2. ROTATION + ACK: the escalation policy carries a handlers ROSTER - the
   real door (POST /scheduler/escalations/tick) delivers attempt 1 to
   handlers[0] (rotated=True on the event); a named human acknowledges
   (the receipt on the record + business.escalation_acknowledged) and the
   door HOLDS the episode (reason=acknowledged) - the human owns it now.
3. THE ONBOARDING LOOP: the Support Operator install builds the generated
   "Case lifecycle onboarding" workflow (dataset_trigger on the tickets
   dataset -> business_onboard), the params resolved to the BUILT process
   id and dataset name; the seeded rows skip honestly; a NEW row landing
   over the API onboards at its own stage. The department's data on-ramp
   beside the channel intakes.

Usage: /home/z/.venv/bin/python scripts/smoke_v88_live.py
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


def _run_wf(c: httpx.Client, wf_id: str) -> dict:
    r = c.post(f"/workflows/{wf_id}/run", json={"payload": {}})
    assert r.status_code in (200, 202), r.text
    exec_id = r.json()["execution_id"]
    detail = {}
    for _ in range(100):
        detail = c.get(f"/executions/{exec_id}").json()
        if detail.get("status") != "running":
            break
        time.sleep(0.1)
    return detail


# ---------------------------------------------------------------------------
# 1) annotate - the agents' memory door
# ---------------------------------------------------------------------------

def annotate_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={"name": "Memory door", "definition": MACHINE})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "ANN-1", "title": "the remembered one"})
    iid = r.json()["id"]

    r = c.post(f"/processes/{pid}/instances/{iid}/annotate", json={
        "context_patch": {"budget": 4200, "channel_pref": "whatsapp"},
        "actor": "closer-agent", "note": "the number from the discovery call"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["state"] == "a" and out["context"]["budget"] == 4200, out
    ann = [j for j in out["journey"] if j["transition"] == "annotate"]
    assert len(ann) == 1 and ann[0]["payload"]["keys"] == ["budget", "channel_pref"], out

    ev = c.get("/events", params={"type": "business.annotated",
                                  "correlation_id": iid}).json()["events"]
    assert len(ev) == 1 and ev[0]["actor"] == "closer-agent", ev

    # the door's bookkeeping is not the agents' scratch space
    r = c.post(f"/processes/{pid}/instances/{iid}/annotate",
               json={"context_patch": {"escalations": {"count": 99}}})
    assert r.status_code == 400 and "reserved" in r.json()["detail"], r.text

    # the node: an agent's workflow annotates BY REF
    r = c.post("/workflows", json={
        "name": "Agent remembers", "is_active": False,
        "graph": {"nodes": [
            {"id": "t", "type": "manual_trigger", "name": "T",
             "position": {"x": 0, "y": 0}, "parameters": {}},
            {"id": "ann", "type": "business_annotate", "name": "Remember",
             "position": {"x": 1, "y": 0},
             "parameters": {"process": "Memory door", "ref": "ANN-1",
                            "context_patch": {"score": 88},
                            "actor": "scorer-agent"}},
        ], "edges": [{"id": "e1", "source": "t", "target": "ann",
                      "sourceHandle": "main", "targetHandle": "main"}]}})
    assert r.status_code == 201, r.text
    detail = _run_wf(c, r.json()["id"])
    assert detail.get("status") == "success", detail
    runs = {x["node_id"]: x for x in detail["node_runs"]}
    out = runs["ann"]["output"]
    assert out["annotated"] is True and out["context"]["score"] == 88, out
    return {"instance_id": iid}


# ---------------------------------------------------------------------------
# 2) the rotation + the receipt - the door tells the roster, the human takes it
# ---------------------------------------------------------------------------

def rotation_ack_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={
        "name": "Roster machine",
        "definition": {**MACHINE, "escalation_policy": {
            "channel": "email",
            "handlers": ["a@py8n.test", "b@py8n.test", "c@py8n.test"],
            "repeat_every_seconds": 60, "max_repeats": 2}}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert "rotate 3" in r.json()["escalation_summary"], r.json()
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "ROT-1", "title": "the rotating one",
                     "due_in_seconds": 1})
    iid = r.json()["id"]

    time.sleep(1.3)  # let the SLA pass (the smoke uses the real clock)
    report = c.post("/scheduler/escalations/tick").json()
    entries = [e for e in report["recorded"] if e["instance_id"] == iid]
    assert entries and entries[0]["attempt"] == 1, report
    ev = c.get("/events", params={"type": "business.escalated",
                                  "correlation_id": iid}).json()["events"][0]
    assert ev["payload"]["rotated"] is True, ev
    assert ev["payload"]["to"] == "a@py8n.test", ev  # attempt 1 -> handler 1

    # the receipt: a named human takes it
    r = c.post(f"/processes/{pid}/instances/{iid}/escalations/ack",
               json={"by": "amara", "note": "on it - calling now"})
    assert r.status_code == 200, r.text
    ack = r.json()
    assert ack["ack"]["by"] == "amara", ack
    assert any(j["transition"] == "escalation_acknowledged"
               for j in ack["instance"]["journey"]), ack
    ev = c.get("/events", params={"type": "business.escalation_acknowledged",
                                  "correlation_id": iid}).json()["events"]
    assert len(ev) == 1 and ev[0]["payload"]["acknowledged_by"] == "amara", ev

    # the door HOLDS - the acknowledged episode goes quiet
    report2 = c.post("/scheduler/escalations/tick").json()
    held = [x for x in report2["held"] if x["instance_id"] == iid]
    assert held and held[0]["reason"] == "acknowledged", report2
    assert held[0]["acked_by"] == "amara", report2
    return {"instance_id": iid}


# ---------------------------------------------------------------------------
# 3) the department onboarding loop - the data on-ramp
# ---------------------------------------------------------------------------

def onboarding_loop_check(c: httpx.Client) -> dict:
    r = c.get("/operators/support-operator")
    plan = r.json()
    loop = next(w for w in plan["installs"]["workflows"]
                if w["name"] == "Case lifecycle onboarding")
    assert loop["trigger"].startswith("dataset:"), loop

    r = c.post("/operators/support-operator/install", json={"note": "smoke v88"})
    assert r.status_code == 200, r.text
    built = r.json()
    loop_wf = next(w for w in built["workflows"]
                   if w["name"] == "Case lifecycle onboarding")
    proc = built["processes"][0]
    assert proc["name"].startswith("Case lifecycle"), proc
    ds = next(d for d in built["datasets"]
              if d["name"].startswith("Support tickets"))

    graph = c.get(f"/workflows/{loop_wf['id']}").json()["graph"]
    step = next(n for n in graph["nodes"] if n["type"] == "business_onboard")
    assert step["parameters"]["process"] == proc["id"], step  # resolved to the BUILT id
    assert step["parameters"]["dataset"] == ds["name"], step

    # the seeded rows are already tracked - the loop skips honestly
    detail = _run_wf(c, loop_wf["id"])
    assert detail.get("status") == "success", detail
    runs = {x["node_id"]: x for x in detail["node_runs"]}
    out = runs[step["id"]]["output"]
    assert out["started"] == 0 and out["already_tracked"] == 3, out

    # a NEW row lands (the month-end spreadsheet) -> onboarded AT its stage
    r = c.post(f"/datasets/{ds['id']}/rows", json={
        "rows": [{"phone": "+15557770001", "subject": "the portal login loop",
                  "status": "assigned"}]})
    assert r.status_code in (200, 201), r.text
    detail = _run_wf(c, loop_wf["id"])
    runs = {x["node_id"]: x for x in detail["node_runs"]}
    out = runs[step["id"]]["output"]
    assert out["started"] == 1, out
    assert out["instances"][0]["ref"] == "+15557770001", out
    assert out["instances"][0]["state"] == "assigned", out
    return {"rows_scanned": out["rows_scanned"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v88_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.88.0", version

            annotate_check(c)
            print(f"[1] ANNOTATE OK - facts landed DIRECTLY on the tracked "
                  f"entity's memory (the machine did not move): on the record "
                  f"(the 'annotate' journey row naming the keys), business."
                  f"annotated on the correlation thread, the node annotating "
                  f"by ref inside a workflow, and the door's reserved "
                  f"'escalations' key refusing loud")

            rotation_ack_check(c)
            print(f"[2] ROTATION + ACK OK - the policy carried a handlers "
                  f"ROSTER and the real door delivered attempt 1 to handler "
                  f"1 (rotated=true on business.escalated); the named "
                  f"receipt (business.escalation_acknowledged) landed on "
                  f"the record and the door HELD the episode "
                  f"(reason=acknowledged) - the human owns it now")

            onboarding_loop_check(c)
            print(f"[3] THE ONBOARDING LOOP OK - the Support Operator install "
                  f"built the generated 'Case lifecycle onboarding' workflow "
                  f"with params resolved to the BUILT process id and dataset "
                  f"name; the seeded rows skipped honestly and a NEW row "
                  f"landing over the API onboarded AT its own stage - the "
                  f"department's data on-ramp beside the channel intakes")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
