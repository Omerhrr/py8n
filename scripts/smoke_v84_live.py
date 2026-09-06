"""V84 live smoke: boot the real server and drive the BUSINESS STATE
MACHINE end to end - long-running autonomy: a machine that remembers.

1. DEFINE + LIFECYCLE: the roadmap's own lead pipeline (lead -> contacted
   -> interested -> demo_booked -> demo_completed -> proposal_sent ->
   negotiating -> won|lost) defines through POST /processes; an instance
   (the tracked lead) starts in 'lead' with a ref + running context, is
   advanced by transition name with a context patch that MERGES (the
   memory keeps what it already knew), and an impossible move refuses
   loud with the allowed moves named.
2. THE BUSINESS MOVING IS OBSERVABLE: a REAL business.state_changed
   event lands in /events on the instance's correlation thread, and an
   event-trigger workflow REACTS to it (a real execution whose shaped
   write lands in the dataset) - the state machine + the event system +
   workflows composing into long-running autonomy.
3. THE PROCESS MEASURED + BOUND: analytics derive instances by state,
   advance counts and mean time in state; the process attaches to a
   system as a first-class component (kind=process) and shows in the
   system's grouped estate.

Usage: /home/z/.venv/bin/python scripts/smoke_v84_live.py
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
API = "http://127.0.0.1:8214/api/v1"
SERVER_PORT = 8214

LEAD_PIPELINE = {
    "states": ["lead", "contacted", "interested", "demo_booked",
               "demo_completed", "proposal_sent", "negotiating", "won", "lost"],
    "initial": "lead",
    "transitions": [
        {"name": "reach_out", "from": "lead", "to": "contacted"},
        {"name": "qualify", "from": "contacted", "to": "interested"},
        {"name": "book_demo", "from": "interested", "to": "demo_booked"},
        {"name": "run_demo", "from": "demo_booked", "to": "demo_completed"},
        {"name": "send_proposal", "from": "demo_completed", "to": "proposal_sent"},
        {"name": "negotiate", "from": "proposal_sent", "to": "negotiating"},
        {"name": "win", "from": "negotiating", "to": "won"},
        {"name": "lose", "from": "negotiating", "to": "lost"},
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


def _drain() -> None:
    time.sleep(1.2)


# ---------------------------------------------------------------------------
# 1) define + lifecycle - the machine remembers
# ---------------------------------------------------------------------------

def lifecycle_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={"name": "Lead pipeline",
                                   "definition": LEAD_PIPELINE,
                                   "description": "the roadmap's own example"})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["initial"] == "lead" and sorted(p["terminal_states"]) == ["lost", "won"], p

    r = c.post(f"/processes/{p['id']}/instances", json={
        "ref": "LEAD-1042", "title": "Globex - Dana Reyes",
        "context": {"source": "website", "plan_interest": "team"},
        "due_in_seconds": 3600})
    assert r.status_code == 201, r.text
    inst = r.json()
    assert inst["state"] == "lead" and inst["context"]["plan_interest"] == "team", inst

    # advance by name - the context patch MERGES into the running memory
    r = c.post(f"/processes/{p['id']}/instances/{inst['id']}/advance", json={
        "transition": "reach_out", "note": "called, voicemail",
        "context_patch": {"calls_made": 1}, "actor": "sdr-1"})
    assert r.status_code == 200, r.text
    inst = r.json()
    assert inst["state"] == "contacted", inst
    assert inst["context"]["calls_made"] == 1 and inst["context"]["plan_interest"] == "team", inst
    assert len(inst["journey"]) == 2, inst  # started + reach_out

    # an impossible move refuses loud, naming the allowed moves
    r = c.post(f"/processes/{p['id']}/instances/{inst['id']}/advance", json={
        "transition": "win"})
    assert r.status_code == 400, r.text
    assert "no transition 'win' from state 'contacted'" in r.json()["detail"], r.text
    assert "qualify" in r.json()["detail"], r.text
    return {"process": p, "instance": inst}


# ---------------------------------------------------------------------------
# 2) the business moving is observable
# ---------------------------------------------------------------------------

def react_check(c: httpx.Client, process_id: str, instance_id: str, ref: str) -> dict:
    r = c.post("/datasets", json={
        "name": f"Follow ups {uuid.uuid4().hex[:6]}",
        "rows": [{"ref": "seed", "to": "seed"}]})
    assert r.status_code == 201, r.text
    ds_id = r.json()["id"]
    graph = {"nodes": [
        {"id": "t", "type": "event_trigger", "name": "Trigger",
         "position": {"x": 0, "y": 0},
         "parameters": {"event_type": "business.state_changed"}},
        {"id": "s", "type": "python_transform", "name": "Shape",
         "position": {"x": 1, "y": 0},
         "parameters": {"code": ("r = df.iloc[0] if len(df) else {}\n"
                                 "pl = r.get('payload') or {}\n"
                                 "result = [{'ref': pl.get('ref', ''), "
                                 "'to': pl.get('to', ''), "
                                 "'transition': pl.get('transition', '')}]")}},
        {"id": "w", "type": "dataset_write", "name": "Write",
         "position": {"x": 2, "y": 0},
         "parameters": {"dataset": c.get(f"/datasets/{ds_id}").json()["name"],
                         "mode": "append"}},
    ], "edges": [
        {"id": "e1", "source": "t", "target": "s",
         "sourceHandle": "main", "targetHandle": "main"},
        {"id": "e2", "source": "s", "target": "w",
         "sourceHandle": "main", "targetHandle": "main"},
    ]}
    r = c.post("/workflows", json={"name": "Follow up on moves",
                                   "graph": graph, "is_active": True})
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]

    # the business moves -> the event fires -> the workflow reacts
    r = c.post(f"/processes/{process_id}/instances/{instance_id}/advance", json={
        "to_state": "interested", "actor": "sdr-1", "note": "qualified"})
    assert r.status_code == 200, r.text
    _drain()

    evs = c.get("/events", params={"type": "business.state_changed",
                                   "correlation_id": instance_id}).json()["events"]
    # BOTH moves are on the instance's thread (the reach_out from check 1
    # and this qualify) - one correlation thread per journey
    assert len(evs) == 2 and all(e["source"] == "business" for e in evs), evs
    latest = sorted(evs, key=lambda e: e["created_at"])[-1]
    assert latest["payload"]["from"] == "contacted" \
        and latest["payload"]["to"] == "interested", latest

    runs = c.get("/executions", params={"limit": 30}).json()
    runs = [x for x in runs if x.get("workflow_id") == wf_id]
    assert len(runs) == 1 and runs[0]["trigger_type"] == "event", runs
    assert runs[0]["status"] == "success", runs[0]

    rows = c.get(f"/datasets/{ds_id}/rows").json()
    items = rows.get("rows") or rows.get("records") or []
    assert len(items) == 2 and items[-1]["ref"] == ref, items
    return {"run": runs[0]["id"], "event": evs[0]["id"]}


# ---------------------------------------------------------------------------
# 3) the process measured + bound
# ---------------------------------------------------------------------------

def analytics_check(c: httpx.Client, process_id: str, instance_id: str) -> None:
    a = c.get(f"/processes/{process_id}/analytics").json()
    assert a["instances"] == 1 and a["open"] == 1, a
    assert a["by_state"].get("interested") == 1, a
    assert a["advance_counts"].get("reach_out") == 1, a
    assert a["mean_time_in_state_seconds"], a

    r = c.post("/systems", json={"name": "Sales system"})
    assert r.status_code in (200, 201), r.text
    sid = r.json()["id"]
    r = c.post(f"/systems/{sid}/components", json={"kind": "process",
                                                   "ref_id": process_id})
    assert r.status_code in (200, 201), r.text
    grouped = c.get(f"/systems/{sid}").json()["grouped"]
    assert grouped.get("process") and grouped["process"][0]["name"] == "Lead pipeline", grouped


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v84_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.84.0", version

            built = lifecycle_check(c)
            print(f"[1] DEFINE + LIFECYCLE OK - the lead pipeline defined "
                  f"(9 states, 8 transitions, terminal won|lost derived); the "
                  f"tracked lead {built['instance']['ref']!r} started in 'lead', "
                  f"advanced by name with the context MERGING (the memory keeps "
                  f"what it knew), and an impossible move refused loud naming "
                  f"the allowed moves")

            r = react_check(c, built["process"]["id"], built["instance"]["id"],
                            built["instance"]["ref"])
            print(f"[2] THE BUSINESS MOVING IS OBSERVABLE OK - the advance to "
                  f"'interested' fired business.state_changed on the instance's "
                  f"correlation thread (event {r['event'][:8]}), and the "
                  f"event-trigger workflow reacted (execution {r['run'][:8]}, "
                  f"the row landed in the dataset) - state machine + events + "
                  f"workflows composing into long-running autonomy")

            analytics_check(c, built["process"]["id"], built["instance"]["id"])
            print(f"[3] THE PROCESS MEASURED + BOUND OK - analytics derived "
                  f"by-state counts, advance counts and mean time in state; "
                  f"the process attached to a system as a first-class "
                  f"component (kind=process) and shows in the grouped estate")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
