"""V105 live smoke: boot the real server and walk the round on the real
wire - the system keys on the process doors, the system's people, and
the front door's own work surface.

1. THE KEYS RIDE THE DOORS: the owner mints py8n_sys_... keys, binds two
   machines to the system, and the key ADVANCES a bound machine over the
   wire (state moved), READS it, is refused by the unbound machine (404)
   and the estate-wide views (403), and the read-only key is refused by
   the move (403).
2. THE SYSTEM'S PEOPLE: an invited EDITOR advances the owner's bound
   machine (the binding is the authority), the VIEWER reads it but their
   move refuses honestly (403).
3. THE WORK SURFACE: the tick door knocks the overdue instance on the
   real clock, GET /systems/by-domain/{domain}/work resolves the live
   domain to the system's pending work (the machine counted, the
   attention row carrying the escalation book), the system's own key
   TAKES the escalation, and the surface answers "taken by".

Usage: /home/z/.venv/bin/python scripts/smoke_v105_live.py
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
KNOCKING = {**MACHINE, "escalation_policy": {
    "channel": "email", "to": "ops@py8n.test",
    "repeat_every_seconds": 60, "max_repeats": 5}}


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


def keys_check(c: httpx.Client, oh: dict, sid: str, m2: dict, m3: dict,
               iid2: str) -> tuple[str, str]:
    r = c.post(f"/systems/{sid}/keys", headers=oh, json={"name": "ERP push job"})
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    r = c.post(f"/systems/{sid}/keys", headers=oh,
               json={"name": "read-only eye", "scopes": ["read"]})
    assert r.status_code == 201, r.text
    ro_key = r.json()["key"]
    sk = {"X-API-Key": key}

    # the key advances the bound machine - the named door, on the wire
    r = c.post(f"/processes/{m2['id']}/instances/{iid2}/advance",
               headers=sk, json={"transition": "go"})
    assert r.status_code == 200 and r.json()["state"] == "b", r.text

    # the key reads its own machine; the unbound one does not exist for it
    r = c.get(f"/processes/{m2['id']}", headers=sk)
    assert r.status_code == 200, r.text
    r = c.get(f"/processes/{m3['id']}", headers=sk)
    assert r.status_code == 404, r.text
    r = c.get(f"/processes/{m3['id']}/instances", headers=sk)
    assert r.status_code == 404, r.text

    # the read-only key may read, never move
    r = c.get(f"/processes/{m2['id']}", headers={"X-API-Key": ro_key})
    assert r.status_code == 200, r.text
    r = c.post(f"/processes/{m2['id']}/instances/{iid2}/advance",
               headers={"X-API-Key": ro_key}, json={"transition": "finish"})
    assert r.status_code == 403, r.text

    # the estate-wide views are human doors
    for door in ("/processes", "/processes/attention", "/processes/chains",
                 "/processes/chain-report"):
        r = c.get(door, headers=sk)
        assert r.status_code == 403, (door, r.text)
    return key, ro_key


def people_check(c: httpx.Client, oh: dict, sid: str, m2: dict) -> None:
    email_e = f"editor-{uuid.uuid4().hex[:6]}@py8n.test"
    email_v = f"viewer-{uuid.uuid4().hex[:6]}@py8n.test"
    r = c.post("/auth/register", json={"email": email_e,
                                       "password": "correct-horse-battery",
                                       "name": "editor"})
    assert r.status_code == 201, r.text
    et = r.json()["token"]
    r = c.post("/auth/register", json={"email": email_v,
                                       "password": "correct-horse-battery",
                                       "name": "viewer"})
    assert r.status_code == 201, r.text
    vt = r.json()["token"]
    r = c.post(f"/systems/{sid}/members", headers=oh,
               json={"email": email_e, "role": "editor"})
    assert r.status_code in (200, 201), r.text
    r = c.post(f"/systems/{sid}/members", headers=oh,
               json={"email": email_v, "role": "viewer"})
    assert r.status_code in (200, 201), r.text

    # a fresh entity for the editor to move
    r = c.post(f"/processes/{m2['id']}/instances", headers=oh,
               json={"ref": "P-2", "due_in_seconds": 3600})
    assert r.status_code == 201, r.text
    iid = r.json()["id"]

    # the EDITOR of the binding system moves the owner's machine
    r = c.post(f"/processes/{m2['id']}/instances/{iid}/advance",
               headers={"Authorization": f"Bearer {et}"},
               json={"transition": "go"})
    assert r.status_code == 200 and r.json()["state"] == "b", r.text

    # the VIEWER reads, but the move refuses honestly
    r = c.get(f"/processes/{m2['id']}",
              headers={"Authorization": f"Bearer {vt}"})
    assert r.status_code == 200, r.text
    r = c.post(f"/processes/{m2['id']}/instances/{iid}/advance",
               headers={"Authorization": f"Bearer {vt}"},
               json={"transition": "finish"})
    assert r.status_code == 403, r.text
    assert "viewer" in r.json()["detail"], r.text


def surface_check(c: httpx.Client, oh: dict, sid: str, domain: str,
                  m1: dict, iid1: str, key: str) -> None:
    # let the 1s SLA pass on the REAL clock, then the tick door knocks
    time.sleep(2.0)
    r = c.post("/scheduler/escalations/tick")
    assert r.status_code == 200, r.text

    r = c.get(f"/systems/by-domain/{domain}/work", headers=oh)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["system"]["id"] == sid and body["domain"] == domain, body
    assert body["my_role"] == "owner", body
    work = body["work"]
    assert work["totals"]["machines"] == 2, work["totals"]
    assert work["totals"]["attention"] == 1, work["totals"]
    row = work["attention"][0]
    assert row["instance_id"] == iid1 and row["ref"] == "LATE-1", row
    assert row["overdue_seconds"] > 0, row
    assert row["escalation"] and row["escalation"]["count"] >= 1, row
    assert row["escalation"]["acked"] is None, row

    # the system's own key TAKES the turn on the surface's row
    r = c.post(
        f"/processes/{m1['id']}/instances/{iid1}/escalations/ack",
        headers={"X-API-Key": key},
        json={"by": "erp-job", "note": "the ERP took the turn"})
    assert r.status_code == 200, r.text
    assert r.json()["ack"]["by"] == "erp-job", r.text

    # and the surface answers "taken by" on the next read
    r = c.get(f"/systems/by-domain/{domain}/work", headers=oh)
    row = r.json()["work"]["attention"][0]
    assert row["escalation"]["acked"]["by"] == "erp-job", row


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v105_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the door
        "PY8N_DEPLOY_PING_OVERRIDE": f"http://127.0.0.1:{SERVER_PORT}/api/v1/health",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v105_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.105.0", version

            # the owner and the estate
            r = c.post("/auth/register", json={
                "email": f"owner-{uuid.uuid4().hex[:6]}@py8n.test",
                "password": "correct-horse-battery", "name": "owner"})
            assert r.status_code == 201, r.text
            oh = {"Authorization": f"Bearer {r.json()['token']}"}

            r = c.post("/systems", headers=oh,
                       json={"name": "Acme Ops v105", "color": "#38bdf8"})
            assert r.status_code == 201, r.text
            sid = r.json()["id"]
            domain = f"ops{uuid.uuid4().hex[:6]}.acme.com"
            r = c.put(f"/systems/{sid}/deployment", headers=oh,
                      json={"domain": domain, "environment": "production"})
            assert r.status_code == 200, r.text
            r = c.post(f"/systems/{sid}/deployment/deploy", headers=oh)
            assert r.status_code == 200, r.text

            # the machines: two bound, one unbound; one of them knocking
            r = c.post("/processes", headers=oh,
                       json={"name": "Late machine v105",
                             "definition": KNOCKING})
            assert r.status_code == 201, r.text
            m1 = r.json()
            r = c.post("/processes", headers=oh,
                       json={"name": "Ready machine v105",
                             "definition": MACHINE})
            assert r.status_code == 201, r.text
            m2 = r.json()
            r = c.post("/processes", headers=oh,
                       json={"name": "Unbound machine v105",
                             "definition": MACHINE})
            assert r.status_code == 201, r.text
            m3 = r.json()
            for m in (m1, m2):
                r = c.post(f"/systems/{sid}/components", headers=oh,
                           json={"kind": "process", "ref_id": m["id"]})
                assert r.status_code in (200, 201), r.text

            r = c.post(f"/processes/{m1['id']}/instances", headers=oh,
                       json={"ref": "LATE-1", "title": "the late one",
                             "due_in_seconds": 1})
            assert r.status_code == 201, r.text
            iid1 = r.json()["id"]
            r = c.post(f"/processes/{m2['id']}/instances", headers=oh,
                       json={"ref": "P-1", "due_in_seconds": 3600})
            assert r.status_code == 201, r.text
            iid2 = r.json()["id"]
            r = c.post(f"/processes/{m3['id']}/instances", headers=oh,
                       json={"ref": "F-1", "due_in_seconds": 3600})
            assert r.status_code == 201, r.text

            key, ro_key = keys_check(c, oh, sid, m2, m3, iid2)
            print(f"[1] KEYS ON THE DOORS OK - py8n_sys_... advanced the bound "
                  f"machine over the wire (state a -> b), read it, the unbound "
                  f"machine 404'd, the read-only key's move 403'd, and the "
                  f"estate-wide views refused the key (403)")
            people_check(c, oh, sid, m2)
            print(f"[2] THE SYSTEM'S PEOPLE OK - the invited editor moved the "
                  f"owner's bound machine (the binding is the authority), the "
                  f"viewer read it and their move refused honestly (403)")
            surface_check(c, oh, sid, domain, m1, iid1, key)
            print(f"[3] THE WORK SURFACE OK - the tick door knocked the overdue "
                  f"instance on the real clock, {domain}/work resolved the "
                  f"system's pending work (2 machines, 1 attention row with "
                  f"the escalation book), the system's own key took the turn, "
                  f"and the surface answered 'taken by erp-job'")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
