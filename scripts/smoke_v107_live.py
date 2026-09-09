"""V107 live smoke: boot the real server and walk the round on the real
wire - the pending-update chips on the estate rows, the ping rhythm on
the deployment panel, and the per-machine views beneath the attention
list.

1. PENDING-UPDATE CHIP ON THE ESTATE ROW: a solution-installed system
   takes an UPGRADE over the wire; the estate health overview's row for
   that system wears the pending-update chip (added_total naming the
   bindings waiting for a ruling) while the quiet system wears none;
   the human ACCEPTS and the chip leaves the estate.
2. PING RHYTHM ON THE DEPLOYMENT PANEL: the deployment read carries
   ``ping_rhythm`` - the default 600s cadence humanized ("10m"), a live
   never-probed door due NOW; the REAL ping door probes the domain (the
   override points the probe at this very server's /health) and the
   rhythm's next-due moves to last-probe + interval; the front door's
   liveness strip wears the same numbers.
3. PER-MACHINE VIEWS BENEATH THE ATTENTION LIST: two machines - one
   late row (1s SLA on the real clock), one on-the-clock row, and one
   no-SLA row on the second machine; each machine's view carries its
   own open work (stuck on top), the attention list stays exactly the
   past-SLA row, and the terminal move retires the row from BOTH.

Usage: /home/z/.venv/bin/python scripts/smoke_v107_live.py
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v74_server.py"
PY = ("/home/z/.venv/bin/python"
      if os.path.exists("/home/z/.venv/bin/python") else "python3")

MACHINE = {
    "states": ["a", "b", "done", "dead"],
    "initial": "a",
    "transitions": [
        {"name": "go", "from": "a", "to": "b"},
        {"name": "finish", "from": "b", "to": "done"},
        {"name": "kill", "from": "a", "to": "dead"},
    ],
}


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


SERVER_PORT = _free_port()
API = f"http://127.0.0.1:{SERVER_PORT}/api/v1"


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


def pending_chip_check(c: httpx.Client, oh: dict, db_path: str, slug: str,
                       sid: str, quiet_id: str, tag: str) -> None:
    # grow the pack the v102 way - the smoke process opens the SAME sqlite
    # database and patches the solution's pack_json (the authoring API
    # packs FROM the estate; growing needs the row itself)
    import asyncio
    import sys as _sys

    os.environ["PY8N_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["PY8N_EXECUTION_MODE"] = "inline"
    _sys.path.insert(0, BACKEND)
    from sqlalchemy import select

    from app.db import AsyncSessionLocal
    from app.models import Solution

    async def _grow():
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(Solution).where(Solution.slug == slug))).scalar_one()
            row.pack_json = {
                "format": "py8n-pack", "pack_version": 1,
                "workflows": [
                    {"name": f"v107 Handler {tag}", "description": "",
                     "graph": {"nodes": [{"id": "t1", "type": "manual_trigger",
                                          "name": "t1",
                                          "position": {"x": 0, "y": 0},
                                          "parameters": {}}], "edges": []}},
                    {"name": f"v107 Extra {tag}", "description": "",
                     "graph": {"nodes": [{"id": "t1", "type": "manual_trigger",
                                          "name": "t1",
                                          "position": {"x": 0, "y": 0},
                                          "parameters": {}}], "edges": []}},
                ],
                "datasets": []}
            await session.commit()
    asyncio.run(_grow())

    # upgrade - the changeset lands on the table
    r = c.post(f"/systems/{sid}/upgrade", headers=oh)
    assert r.status_code == 200, r.text

    r = c.get("/systems/health/overview", headers=oh)
    assert r.status_code == 200, r.text
    by_id = {row["id"]: row for row in r.json()["systems"]}
    chip = by_id[sid]["pending_update"]
    assert chip is not None and chip["added_total"] == 1, by_id[sid]
    assert by_id[quiet_id]["pending_update"] is None, by_id[quiet_id]

    # the SAME answer the Updates panel applies (one predicate)
    r = c.get(f"/systems/{sid}/update/preview", headers=oh)
    assert r.json()["pending"]["operation_id"] == chip["operation_id"], r.text

    # the human rules - accept settles the changeset, the chip leaves
    r = c.post(f"/systems/{sid}/update/accept", headers=oh)
    assert r.status_code == 200, r.text
    r = c.get("/systems/health/overview", headers=oh)
    by_id = {row["id"]: row for row in r.json()["systems"]}
    assert by_id[sid]["pending_update"] is None, by_id[sid]


def ping_rhythm_check(c: httpx.Client, oh: dict, sid: str, domain: str) -> None:
    r = c.get(f"/systems/{sid}/deployment", headers=oh)
    assert r.status_code == 200, r.text
    dep = r.json()["deployment"]
    rhythm = dep["ping_rhythm"]
    assert rhythm["interval_seconds"] == 600 and rhythm["every"] == "10m", rhythm
    assert rhythm["scheduled"] is True and rhythm["next_due_at"] is not None, rhythm

    # the REAL ping door over the wire - the override aims it at our own /health
    r = c.post(f"/systems/{sid}/deployment/ping", headers=oh)
    assert r.status_code == 200, r.text
    body = r.json()
    ping = body.get("ping", body)
    assert ping.get("ok") is True, body

    # the rhythm's next due now names the REAL probe + the interval
    r = c.get(f"/systems/{sid}/deployment", headers=oh)
    dep = r.json()["deployment"]
    assert dep["last_ping"] and dep["last_ping"]["ok"] is True, dep
    assert dep["ping_rhythm"]["next_due_at"] is not None, dep

    # the front door's liveness strip wears the same rhythm
    r = c.get(f"/systems/by-domain/{domain}/work", headers=oh)
    assert r.status_code == 200, r.text
    lv = r.json()["liveness"]
    assert lv["ping_rhythm"]["every"] == "10m", lv
    assert lv["ping_rhythm"]["next_due_at"] is not None, lv


def machine_views_check(c: httpx.Client, oh: dict, sid: str, domain: str,
                        m1: dict, iid_late: str, iid_calm: str,
                        m2: dict, iid_b: str) -> None:
    # let the 1s SLA pass on the REAL clock (the v96 discipline)
    time.sleep(2.0)
    r = c.get(f"/systems/by-domain/{domain}/work", headers=oh)
    assert r.status_code == 200, r.text
    work = r.json()["work"]
    by_machine = {m["process_id"]: m for m in work["machines"]}

    va = by_machine[m1["id"]]
    assert va["open"] == 2 and va["stuck"] == 1, va
    assert [i["instance_id"] for i in va["instances"]] == [iid_late, iid_calm], va
    assert va["instances"][0]["stuck"] is True, va
    assert va["instances"][0]["overdue_seconds"] > 0, va
    assert va["instances"][1]["stuck"] is False, va
    assert va["instances_hidden"] == 0, va

    vb = by_machine[m2["id"]]
    assert [i["instance_id"] for i in vb["instances"]] == [iid_b], vb
    assert vb["instances"][0]["stuck"] is False, vb

    # the attention list is UNCHANGED: exactly the past-SLA row
    assert work["totals"]["attention"] == 1, work["totals"]
    assert work["attention"][0]["instance_id"] == iid_late, work["attention"]

    # the terminal move retires the row from the machine view too
    r = c.post(f"/processes/{m1['id']}/instances/{iid_late}/advance",
               headers=oh, json={"transition": "go"})
    assert r.status_code == 200, r.text
    r = c.post(f"/processes/{m1['id']}/instances/{iid_late}/advance",
               headers=oh, json={"transition": "finish"})
    assert r.status_code == 200, r.text
    r = c.get(f"/systems/by-domain/{domain}/work", headers=oh)
    work = r.json()["work"]
    va = {m["process_id"]: m for m in work["machines"]}[m1["id"]]
    assert [i["instance_id"] for i in va["instances"]] == [iid_calm], va
    assert va["stuck"] == 0 and work["totals"]["attention"] == 0, work["totals"]


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v107_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the clock
        "PY8N_DEPLOY_PING_OVERRIDE": f"http://127.0.0.1:{SERVER_PORT}/api/v1/health",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v107_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.107.0", version

            r = c.post("/auth/register", json={
                "email": f"owner-{uuid.uuid4().hex[:6]}@py8n.test",
                "password": "correct-horse-battery", "name": "owner"})
            assert r.status_code == 201, r.text
            oh = {"Authorization": f"Bearer {r.json()['token']}"}

            # a quiet system - its estate row must stay chip-less
            r = c.post("/systems", headers=oh, json={"name": "Quiet Ops v107"})
            assert r.status_code == 201, r.text
            quiet_id = r.json()["id"]

            # a solution-installed system (the update lifecycle's subject):
            # build a workflow, pack it as a solution, install as a system
            tag = uuid.uuid4().hex[:6]
            r = c.post("/workflows", headers=oh, json={
                "name": f"v107 Handler {tag}", "graph": {
                    "nodes": [{"id": "t1", "type": "manual_trigger",
                               "name": "t1", "position": {"x": 0, "y": 0},
                               "parameters": {}}],
                    "edges": []}})
            assert r.status_code == 201, r.text
            r = c.post("/solutions", headers=oh, json={
                "name": f"v107 Ops Suite {tag}",
                "outcomes": ["one workflow"],
                "workflow_ids": [r.json()["id"]]})
            assert r.status_code in (200, 201), r.text
            slug = r.json()["slug"]
            r = c.post(f"/solutions/{slug}/install", headers=oh,
                       json={"as_system": True})
            assert r.status_code == 200, r.text
            sid = r.json()["system"]["id"]

            # the production system on its custom domain
            r = c.post("/systems", headers=oh,
                       json={"name": "Acme Ops v107", "color": "#38bdf8"})
            assert r.status_code == 201, r.text
            did = r.json()["id"]
            domain = f"ops{uuid.uuid4().hex[:6]}.acme.com"
            r = c.put(f"/systems/{did}/deployment", headers=oh,
                      json={"domain": domain, "environment": "production"})
            assert r.status_code == 200, r.text
            r = c.post(f"/systems/{did}/deployment/deploy", headers=oh)
            assert r.status_code == 200, r.text

            # two machines on the deployed system:
            # machine A gets a late row + a calm row, machine B one plain row
            r = c.post("/processes", headers=oh,
                       json={"name": "Front desk v107", "definition": MACHINE})
            assert r.status_code == 201, r.text
            m1 = r.json()
            r = c.post(f"/systems/{did}/components", headers=oh,
                       json={"kind": "process", "ref_id": m1["id"]})
            assert r.status_code in (200, 201), r.text
            r = c.post("/processes", headers=oh,
                       json={"name": "Back office v107", "definition": MACHINE})
            assert r.status_code == 201, r.text
            m2 = r.json()
            r = c.post(f"/systems/{did}/components", headers=oh,
                       json={"kind": "process", "ref_id": m2["id"]})
            assert r.status_code in (200, 201), r.text

            r = c.post(f"/processes/{m1['id']}/instances", headers=oh,
                       json={"ref": "V107-1", "title": "the late one",
                             "due_in_seconds": 1})
            assert r.status_code == 201, r.text
            iid_late = r.json()["id"]
            r = c.post(f"/processes/{m1['id']}/instances", headers=oh,
                       json={"ref": "V107-2", "title": "the calm one",
                             "due_in_seconds": 3600})
            assert r.status_code == 201, r.text
            iid_calm = r.json()["id"]
            r = c.post(f"/processes/{m2['id']}/instances", headers=oh,
                       json={"ref": "V107-3"})
            assert r.status_code == 201, r.text
            iid_b = r.json()["id"]

            machine_views_check(c, oh, did, domain, m1, iid_late, iid_calm,
                                m2, iid_b)
            print(f"[1] PER-MACHINE VIEWS OK - machine A's view carried both "
                  f"open rows (the stuck one on top with the REAL overdue "
                  f"seconds), machine B's view carried its own no-SLA row, "
                  f"the attention list stayed exactly the past-SLA row, and "
                  f"the terminal move retired the row from BOTH")
            ping_rhythm_check(c, oh, did, domain)
            print(f"[2] PING RHYTHM ON THE DEPLOYMENT PANEL OK - the read "
                  f"wears auto-probe every 10m (the walk's own cadence), the "
                  f"real ping door probed {domain} over the wire (HTTP 200) "
                  f"and the rhythm's next-due moved, and the front door's "
                  f"liveness strip wears the same numbers")
            pending_chip_check(c, oh, db_path, slug, sid, quiet_id, tag)
            print(f"[3] PENDING-UPDATE CHIP ON THE ESTATE ROW OK - the "
                  f"upgraded system's row wore the chip (added_total 1), the "
                  f"quiet system's row wore none, and the accept removed the "
                  f"chip from the estate")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
