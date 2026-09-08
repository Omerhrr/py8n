"""V102 live smoke: boot the real server and walk the round on the real
wire - the deployed-system identity, the estate health overview, and
the operator update lifecycle.

1. THE DEPLOYED IDENTITY: a system gets a deployment record (domain
   normalized lowercase, environment production), DEPLOYS (the loud
   verb stamps deployed_at and writes the operations log), and the
   PUBLIC domain door resolves the domain back to the identity a login
   surface shows before authentication. A second system's claim on the
   same domain is a 409; pausing turns the surface dark (honest 404);
   retiring brings it back offline.
2. THE ESTATE HEALTH + THE UPDATE LIFECYCLE: installing the sales
   operator puts a row in /systems/health/overview; an overdue open
   instance flips it to attention (the sketch made real); a solution
   installed as a system previews its upgrade as IDEMPOTENT, the pack
   grows, the preview names exactly what will change, the upgrade puts
   the changeset PENDING, and accept settles it (rollback then refuses
   loud).

Usage: /home/z/.venv/bin/python scripts/smoke_v102_live.py
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


# ---------------------------------------------------------------------------
# 1) the deployed identity over the wire
# ---------------------------------------------------------------------------

def deployment_identity_check(c: httpx.Client) -> str:
    r = c.post("/systems", json={"name": "Acme Sales", "color": "#38bdf8",
                                 "description": "The Acme revenue line"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    # nothing is deployed yet, and deploying before configuring refuses
    r = c.get(f"/systems/{sid}/deployment")
    assert r.status_code == 200 and r.json()["deployment"] is None, r.text
    r = c.post(f"/systems/{sid}/deployment/deploy")
    assert r.status_code == 400 and "no deployment record" in r.json()["detail"], r.text

    # the identity saves NORMALIZED, and the record starts offline
    r = c.put(f"/systems/{sid}/deployment",
              json={"domain": "Sales.Acme.COM.", "environment": "production",
                    "branding": {"accent": "#38bdf8", "tagline": "The Acme revenue line",
                                 "login_headline": "Acme Operations"}})
    assert r.status_code == 200, r.text
    dep = r.json()["deployment"]
    assert dep["domain"] == "sales.acme.com" and dep["status"] == "offline", dep
    assert dep["url"] == "https://sales.acme.com", dep

    # unusable + reserved names refuse loud
    for bad in ("ops", "localhost", "foo.local", "example.com"):
        r = c.post("/systems", json={"name": f"throwaway {bad}"})
        other = r.json()["id"]
        r = c.put(f"/systems/{other}/deployment", json={"domain": bad})
        assert r.status_code == 400, (bad, r.text)

    # DEPLOY - the loud verb: live, stamped, on the record
    r = c.post(f"/systems/{sid}/deployment/deploy")
    assert r.status_code == 200, r.text
    dep = r.json()["deployment"]
    assert dep["status"] == "live" and dep["deployed_at"], dep
    r = c.get(f"/systems/{sid}/operations")
    assert any(o["verb"] == "deployed" for o in r.json()["operations"]), r.text

    # the PUBLIC domain door: the identity before authentication
    r = c.get("/systems/by-domain/sales.acme.com")
    assert r.status_code == 200, r.text
    ident = r.json()
    assert ident["system"]["name"] == "Acme Sales", ident
    assert ident["environment"] == "production" and ident["branding"]["accent"], ident

    # one domain, one system
    r = c.post("/systems", json={"name": "Acme Finance"})
    other = r.json()["id"]
    r = c.put(f"/systems/{other}/deployment", json={"domain": "sales.acme.com"})
    assert r.status_code == 409, r.text

    # pausing turns the surface dark - an honest 404, not a pretty lie
    r = c.post(f"/systems/{sid}/deployment/pause")
    assert r.status_code == 200 and r.json()["deployment"]["status"] == "paused", r.text
    r = c.get("/systems/by-domain/sales.acme.com")
    assert r.status_code == 404 and "not live" in r.json()["detail"], r.text
    r = c.post(f"/systems/{sid}/deployment/retire")
    assert r.status_code == 200 and r.json()["deployment"]["status"] == "offline", r.text
    return sid


# ---------------------------------------------------------------------------
# 2) the estate health + the update lifecycle over the wire
# ---------------------------------------------------------------------------

def health_and_update_check(c: httpx.Client, db_path: str, sid: str) -> dict:
    # the operator install puts a system on the board; the deployed one too
    r = c.post("/operators/sales-operator/install", json={"note": "smoke v102"})
    assert r.status_code == 200, r.text

    # a fresh estate is green
    r = c.get("/systems/health/overview")
    assert r.status_code == 200, r.text
    rows = {row["name"]: row for row in r.json()["systems"]}
    assert "Acme Sales" in rows and "Sales Operator" in rows, list(rows)
    assert rows["Acme Sales"]["deployment"]["domain"] == "sales.acme.com", rows["Acme Sales"]
    assert rows["Acme Sales"]["status"] == "running", rows["Acme Sales"]

    # an overdue open instance flips the operator's system to attention
    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    r = c.post(f"/processes/{procs['Lead pipeline']}/instances",
               json={"ref": "+15551010202", "title": "v102 Attention Deal",
                     "due_in_seconds": 1})
    assert r.status_code == 201, r.text
    time.sleep(2.0)  # the SLA clock runs on the real time here
    r = c.get("/systems/health/overview")
    rows = {row["name"]: row for row in r.json()["systems"]}
    assert rows["Sales Operator"]["status"] == "attention", rows["Sales Operator"]
    assert rows["Sales Operator"]["overdue"] >= 1, rows["Sales Operator"]

    # THE UPDATE LIFECYCLE: build a workflow, pack it as a solution,
    # install as a system - the preview is IDEMPOTENT
    tag = uuid.uuid4().hex[:6]
    r = c.post("/workflows", json={"name": f"v102 Handler {tag}", "graph": {
        "nodes": [{"id": "t1", "type": "manual_trigger", "name": "t1",
                   "position": {"x": 0, "y": 0}, "parameters": {}}],
        "edges": []}})
    assert r.status_code == 201, r.text
    wf1 = r.json()["id"]
    r = c.post("/solutions", json={
        "name": f"v102 Ops Suite {tag}", "outcomes": ["one workflow"],
        "workflow_ids": [wf1]})
    assert r.status_code == 201, r.text
    slug = r.json()["slug"]
    r = c.post(f"/solutions/{slug}/install", json={"as_system": True})
    assert r.status_code == 200, r.text
    up_sid = r.json()["system"]["id"]

    r = c.get(f"/systems/{up_sid}/update/preview")
    assert r.status_code == 200, r.text
    prev = r.json()
    assert prev["updatable"] is True and prev["idempotent"] is True, prev
    assert prev["pending"] is None, prev

    # the pack GROWS (a direct row edit - the pack is a JSON document):
    # now the preview names exactly what will change
    import asyncio
    os.environ["PY8N_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["PY8N_EXECUTION_MODE"] = "inline"
    sys.path.insert(0, BACKEND)
    from sqlalchemy import select

    from app.db import AsyncSessionLocal
    from app.models import Solution, Workflow

    async def _grow():
        async with AsyncSessionLocal() as session:
            wf2 = Workflow(name=f"v102 Extra {tag}", graph={
                "nodes": [{"id": "t1", "type": "manual_trigger", "name": "t1",
                           "position": {"x": 0, "y": 0}, "parameters": {}}],
                "edges": []}, is_active=True, owner_id=None)
            session.add(wf2)
            await session.flush()
            row = (await session.execute(
                select(Solution).where(Solution.slug == slug))).scalar_one()
            row.pack_json = {
                "format": "py8n-pack", "pack_version": 1,
                "workflows": [
                    {"name": f"v102 Handler {tag}", "description": "",
                     "graph": {"nodes": [{"id": "t1", "type": "manual_trigger",
                                          "name": "t1", "position": {"x": 0, "y": 0},
                                          "parameters": {}}], "edges": []}},
                    {"name": f"v102 Extra {tag}", "description": "",
                     "graph": {"nodes": [{"id": "t1", "type": "manual_trigger",
                                          "name": "t1", "position": {"x": 0, "y": 0},
                                          "parameters": {}}], "edges": []}},
                ],
                "datasets": []}
            await session.commit()
    asyncio.run(_grow())

    r = c.get(f"/systems/{up_sid}/update/preview")
    prev = r.json()
    assert prev["idempotent"] is False, prev
    assert prev["adds"]["workflow"] == [f"v102 Extra {tag}"], prev

    # APPLY -> PENDING -> ACCEPT (rollback then refuses loud)
    r = c.post(f"/systems/{up_sid}/upgrade")
    assert r.status_code == 200 and r.json()["added"]["workflow"] == 1, r.text
    r = c.get(f"/systems/{up_sid}/update/preview")
    pending = r.json()["pending"]
    assert pending and [w["name"] for w in pending["added_refs"]["workflow"]] == [f"v102 Extra {tag}"], r.text
    r = c.post(f"/systems/{up_sid}/update/accept")
    assert r.status_code == 200 and r.json()["accepted"] is True, r.text
    r = c.get(f"/systems/{up_sid}/update/preview")
    assert r.json()["pending"] is None, r.text
    r = c.post(f"/systems/{up_sid}/update/accept")
    assert r.status_code == 409, r.text
    r = c.get(f"/systems/{up_sid}/operations")
    verbs = [o["verb"] for o in r.json()["operations"]]
    assert "upgrade_accepted" in verbs, verbs
    return {"domain": "sales.acme.com", "accepted": True}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v102_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the door
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v102_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.102.0", version

            sid = deployment_identity_check(c)
            print(f"[1] DEPLOYED IDENTITY OK - the domain normalized and saved, "
                  f"the DEPLOY verb went live and stamped the record, the PUBLIC "
                  f"domain door answered the identity before authentication, a "
                  f"second claim on the same domain was a 409, and pausing turned "
                  f"the surface dark (honest 404)")
            health_and_update_check(c, db_path, sid)
            print(f"[2] ESTATE HEALTH + UPDATE LIFECYCLE OK - the overview carried "
                  f"the deployed identity and flipped to attention when an SLA "
                  f"broke; the solution-installed system previewed its upgrade "
                  f"(idempotent first, then exactly the pack's growth), the "
                  f"upgrade went PENDING, and accept settled it (a second ruling "
                  f"refused loud)")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
