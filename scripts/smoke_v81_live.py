"""V81 live smoke: boot the real server and drive the SYSTEM RUNTIME end
to end - systems as running entities with a lifecycle, an operations log
and solution provenance.

1. INSTALL AS A RUNNING SYSTEM: the support-line-system solution installs
   with as_system + as_voice_agent + as_support_line - the system binds
   the whole topology (workflow handler + knowledge dataset + voice agent
   + queue + room), lands RUNNING with source_solution_slug stamped, and
   the install is on the record (operation + system.installed event).
2. THE GATE: a bound event-trigger workflow reacts while the system is
   running; STOP the system and the same event dispatches NOTHING while
   the workflow's own is_active stays untouched; START (with the boot
   door) reopens the gate and activates the pack-installed inactive
   workflows loudly, on the record.
3. OPERATIONS + STATE + UPGRADE: every verb lands in the operations log
   and on the system.* event thread; the state snapshot reports the gate,
   the live room and last activity; the upgrade re-applies the solution's
   pack and reconciles honestly (idempotent - everything already bound,
   nothing imported, nothing rewritten).

Usage: /home/z/.venv/bin/python scripts/smoke_v81_live.py
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
API = "http://127.0.0.1:8211/api/v1"
SERVER_PORT = 8211


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


def _ops(c: httpx.Client, sid: str) -> list[str]:
    return [o["verb"] for o in c.get(f"/systems/{sid}/operations").json()["operations"]]


def _sys_event_types(c: httpx.Client, sid: str) -> list[str]:
    evs = c.get(f"/systems/{sid}/events").json()["events"]
    return [e["type"] for e in evs]


# ---------------------------------------------------------------------------
# 1) install as a running system
# ---------------------------------------------------------------------------

def install_check(c: httpx.Client, tag: str) -> dict:
    res = c.post("/solutions/support-line-system/install", json={
        "as_system": True, "as_voice_agent": True, "as_support_line": True,
        "note": "the v81 runtime smoke"})
    assert res.status_code in (200, 201), res.text
    sysref = res.json()["system"]
    counts = sysref["components"]
    assert sysref["lifecycle"] == "running", sysref
    assert counts.get("workflow", 0) >= 1 and counts.get("dataset", 0) >= 1
    assert counts.get("voice_agent") == 1 and counts.get("queue") == 1 \
        and counts.get("meeting") == 1, counts

    sid = sysref["id"]
    detail = c.get(f"/systems/{sid}").json()
    assert detail["source_solution_slug"] == "support-line-system"
    assert _ops(c, sid)[-1] == "installed"
    assert "system.installed" in _sys_event_types(c, sid)

    state = c.get(f"/systems/{sid}/state").json()
    assert state["workflows"]["active"] == 0  # pack honesty: landed inactive
    assert state["live"]["live_meetings"] == 1  # the installed room is live
    return {"sid": sid, "counts": counts}


# ---------------------------------------------------------------------------
# 2) the gate: react -> stop -> hold -> boot
# ---------------------------------------------------------------------------

def gate_check(c: httpx.Client, tag: str, sid: str) -> dict:
    graph = {"nodes": [
        {"id": "t", "type": "event_trigger", "name": "t",
         "position": {"x": 0, "y": 0},
         "parameters": {"event_type": "smoke.*"}},
        {"id": "c", "type": "code", "name": "c",
         "position": {"x": 120, "y": 0},
         "parameters": {"mode": "run_once_for_each_item",
                        "jsCode": "return {saw: input.event.type};"}},
    ], "edges": [{"id": "e1", "source": "t", "target": "c",
                  "sourceHandle": "main", "targetHandle": "main"}]}
    res = c.post("/workflows", json={"name": f"system reactor {tag}",
                                     "graph": graph, "is_active": True})
    assert res.status_code == 201, res.text
    wf_id = res.json()["id"]
    res = c.post(f"/systems/{sid}/components", json={"kind": "workflow", "ref_id": wf_id})
    assert res.status_code == 201, res.text

    def _runs() -> int:
        return len([r for r in c.get("/executions", params={"limit": 50}).json()
                    if r.get("workflow_id") == wf_id])

    def _wait_runs(target: int, deadline: float = 20.0) -> int:
        end = time.time() + deadline
        while time.time() < end:
            if _runs() >= target:
                return _runs()
            time.sleep(0.4)
        raise SystemExit(f"the event trigger never reached {target} runs")

    # running: the event dispatches
    c.post("/events", json={"type": "smoke.gate", "source": "user", "payload": {"n": 1}})
    _wait_runs(1)

    # stop: the gate holds the SAME trigger without touching is_active
    assert c.post(f"/systems/{sid}/stop", json={}).status_code == 200
    c.post("/events", json={"type": "smoke.gate", "source": "user", "payload": {"n": 2}})
    time.sleep(2.5)
    assert _runs() == 1, "the gate did not hold the trigger"
    wf = c.get(f"/workflows/{wf_id}").json()
    assert wf["is_active"] is True  # the flag was never touched
    state = c.get(f"/systems/{sid}/state").json()
    assert state["lifecycle"] == "stopped" and state["workflows"]["gate_blocked"] >= 1

    # boot: start with activate_workflows flips the pack-installed inactive
    # workflows ON - loudly, on the record
    res = c.post(f"/systems/{sid}/start", json={"activate_workflows": True})
    assert res.status_code == 200 and res.json()["workflows_activated"] >= 1, res.text
    state = c.get(f"/systems/{sid}/state").json()
    assert state["workflows"]["active"] >= 1
    c.post("/events", json={"type": "smoke.gate", "source": "user", "payload": {"n": 3}})
    _wait_runs(2)
    return {"runs": 2}


# ---------------------------------------------------------------------------
# 3) operations + state + the idempotent upgrade
# ---------------------------------------------------------------------------

def upgrade_check(c: httpx.Client, tag: str, sid: str) -> dict:
    verbs = _ops(c, sid)
    # the journey left its audit trail: installed, component_added, stop, start
    assert {"installed", "component_added", "stop", "start"} <= set(verbs), verbs
    types = _sys_event_types(c, sid)
    assert {"system.installed", "system.component_added",
            "system.stopped", "system.started"} <= set(types), types

    res = c.post(f"/systems/{sid}/upgrade", json={})
    assert res.status_code == 200, res.text
    up = res.json()
    assert up["solution"] == "support-line-system"
    assert up["imports_skipped"] is True, up
    assert up["added"] == {"workflow": 0, "dataset": 0}
    assert "already bound" in up["note"]
    assert _ops(c, sid)[0] == "upgraded"
    assert "system.upgraded" in _sys_event_types(c, sid)

    detail = c.get(f"/systems/{sid}").json()
    assert detail["upgraded_at"]
    return {"upgraded_at": True, "verbs": len(verbs) + 1}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v81_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.81.0", version
            tag = uuid.uuid4().hex[:6]

            i = install_check(c, tag)
            print(f"[1] INSTALL AS A RUNNING SYSTEM OK - support-line-system "
                  f"installed with as_system: the system bound {i['counts']} and "
                  f"landed RUNNING with the solution stamped and the install on "
                  f"the record (operation + system.installed event)")

            g = gate_check(c, tag, i["sid"])
            print(f"[2] THE GATE OK - the bound event-trigger workflow reacted "
                  f"while running ({g['runs']} runs), STOP held the same trigger "
                  f"without touching is_active, and START with the boot door "
                  f"activated the pack-installed workflows and reopened the gate")

            u = upgrade_check(c, tag, i["sid"])
            print(f"[3] OPERATIONS + STATE + UPGRADE OK - {u['verbs']} verbs on the "
                  f"operations log and the system.* event thread, the state "
                  f"snapshot reported the gate and the live room, and the "
                  f"idempotent upgrade re-applied the pack honestly: everything "
                  f"already bound, nothing imported, nothing rewritten")

            print(f"\nALL 3 CHECKS GREEN - v81 live smoke passed (version {version})")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        for f in (db_path, db_path + "-wal", db_path + "-shm"):
            try:
                os.unlink(f)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
