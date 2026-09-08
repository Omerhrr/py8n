"""V106 live smoke: boot the real server and walk the round on the real
wire - the advance actions beside the acks on the surface, the
per-domain TLS notes on the route sheet, and the deployment liveness
riding the surface.

1. ADVANCE BESIDE THE ACKS: an overdue entity lands on the work surface
   wearing the moves the machine allows FROM its state; the owner
   ADVANCES it over the wire (the journey names the human), the row
   re-reads wearing the new state's moves, and the terminal move
   retires it from the attention list entirely.
2. TLS NOTES ON THE ROUTE SHEET: the real ping door probes the domain
   (the override points the probe at this very server's /health - HTTP
   200 over the wire), and the derived Caddy sheet's block for that
   domain carries "certificate live" with the probe's numbers; a
   staging deployment never probed reads the rehearsal issuer + an
   honest "no probe evidence yet".
3. LIVENESS RIDING THE SURFACE: the work surface answers "liveness" -
   before the probe it was an honest null; after it, the evidence is
   green with the code and the milliseconds the real probe saw.

Usage: /home/z/.venv/bin/python scripts/smoke_v106_live.py
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


def surface_advance_check(c: httpx.Client, oh: dict, sid: str, domain: str,
                          m1: dict, iid1: str) -> None:
    # let the 1s SLA pass on the REAL clock (the v96 discipline)
    time.sleep(2.0)
    r = c.get(f"/systems/by-domain/{domain}/work", headers=oh)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["system"]["id"] == sid and body["domain"] == domain, body
    work = body["work"]
    assert work["totals"]["attention"] == 1, work["totals"]
    row = work["attention"][0]
    assert row["instance_id"] == iid1 and row["state"] == "a", row
    assert row["transitions"] == [
        {"name": "go", "to": "b"}, {"name": "kill", "to": "dead"}], row

    # liveness rides the surface - never probed yet: an honest null
    lv = body["liveness"]
    assert lv["domain"] == domain and lv["status"] == "live", lv
    assert lv["last_ping"] is None, lv

    # the owner advances from the surface's door - the move names them
    r = c.post(f"/processes/{m1['id']}/instances/{iid1}/advance",
               headers=oh, json={"transition": "go", "actor": "owner-amara",
                                 "note": "picked it up"})
    assert r.status_code == 200 and r.json()["state"] == "b", r.text
    actors = [j.get("actor") for j in r.json().get("journey", [])]
    assert "owner-amara" in actors, actors

    # still late, still on the list - wearing state b's moves now
    r = c.get(f"/systems/by-domain/{domain}/work", headers=oh)
    row = r.json()["work"]["attention"][0]
    assert row["state"] == "b", row
    assert row["transitions"] == [{"name": "finish", "to": "done"}], row

    # the terminal move retires the row from the attention list
    r = c.post(f"/processes/{m1['id']}/instances/{iid1}/advance",
               headers=oh, json={"transition": "finish"})
    assert r.status_code == 200, r.text
    r = c.get(f"/systems/by-domain/{domain}/work", headers=oh)
    assert r.json()["work"]["totals"]["attention"] == 0, r.text


def tls_notes_check(c: httpx.Client, oh: dict, sid: str, domain: str,
                    sid_stage: str, domain_stage: str) -> None:
    # the REAL ping door over the wire - the override aims it at our own /health
    r = c.post(f"/systems/{sid}/deployment/ping", headers=oh)
    assert r.status_code == 200, r.text
    ping = r.json()["ping"] if "ping" in r.json() else r.json()
    assert (ping.get("result", ping) or {}).get("ok") is True or ping.get("ok") is True, r.text

    r = c.get("/systems/deployment/routes.caddy", headers=oh)
    assert r.status_code == 200, r.text
    sheet = r.text
    assert "# tls: every site block relies on Caddy's automatic HTTPS" in sheet, sheet
    block = sheet.split(f"{domain} {{")[1].split("\n}")[0]
    assert "certificate live: last probe answered HTTP 200 in" in block, block
    assert f"rewrite * /go/{domain}" in block

    block_stage = sheet.split(f"{domain_stage} {{")[1].split("\n}")[0]
    assert "staging - rehearse on the ACME staging issuer" in block_stage, block_stage
    assert "no probe evidence yet" in block_stage, block_stage


def liveness_check(c: httpx.Client, oh: dict, domain: str) -> None:
    r = c.get(f"/systems/by-domain/{domain}/work", headers=oh)
    assert r.status_code == 200, r.text
    lv = r.json()["liveness"]
    assert lv["last_ping"] and lv["last_ping"]["ok"] is True, lv
    assert lv["last_ping"]["code"] == 200, lv
    assert lv["last_ping"]["ms"] >= 0, lv


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v106_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the clock
        "PY8N_DEPLOY_PING_OVERRIDE": f"http://127.0.0.1:{SERVER_PORT}/api/v1/health",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v106_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.106.0", version

            r = c.post("/auth/register", json={
                "email": f"owner-{uuid.uuid4().hex[:6]}@py8n.test",
                "password": "correct-horse-battery", "name": "owner"})
            assert r.status_code == 201, r.text
            oh = {"Authorization": f"Bearer {r.json()['token']}"}

            # the production system on its custom domain
            r = c.post("/systems", headers=oh,
                       json={"name": "Acme Ops v106", "color": "#38bdf8"})
            assert r.status_code == 201, r.text
            sid = r.json()["id"]
            domain = f"ops{uuid.uuid4().hex[:6]}.acme.com"
            r = c.put(f"/systems/{sid}/deployment", headers=oh,
                      json={"domain": domain, "environment": "production"})
            assert r.status_code == 200, r.text
            r = c.post(f"/systems/{sid}/deployment/deploy", headers=oh)
            assert r.status_code == 200, r.text

            # the rehearsal system - never probed
            r = c.post("/systems", headers=oh,
                       json={"name": "Rehearsal Ops v106"})
            assert r.status_code == 201, r.text
            sid_stage = r.json()["id"]
            domain_stage = f"stage{uuid.uuid4().hex[:6]}.acme.com"
            r = c.put(f"/systems/{sid_stage}/deployment", headers=oh,
                      json={"domain": domain_stage, "environment": "staging"})
            assert r.status_code == 200, r.text
            r = c.post(f"/systems/{sid_stage}/deployment/deploy", headers=oh)
            assert r.status_code == 200, r.text

            # a bound machine with an overdue entity
            r = c.post("/processes", headers=oh,
                       json={"name": "Late machine v106",
                             "definition": MACHINE})
            assert r.status_code == 201, r.text
            m1 = r.json()
            r = c.post(f"/systems/{sid}/components", headers=oh,
                       json={"kind": "process", "ref_id": m1["id"]})
            assert r.status_code in (200, 201), r.text
            r = c.post(f"/processes/{m1['id']}/instances", headers=oh,
                       json={"ref": "ADV-1", "title": "the late one",
                             "due_in_seconds": 1})
            assert r.status_code == 201, r.text
            iid1 = r.json()["id"]

            surface_advance_check(c, oh, sid, domain, m1, iid1)
            print(f"[1] ADVANCE BESIDE THE ACKS OK - the overdue row wore the "
                  f"machine's own moves [go->b, kill->dead], the owner advanced "
                  f"it over the wire (the journey named owner-amara), the row "
                  f"re-read wearing state b's moves, and the terminal move "
                  f"retired it from the attention list")
            tls_notes_check(c, oh, sid, domain, sid_stage, domain_stage)
            print(f"[2] PER-DOMAIN TLS NOTES OK - the real ping door probed "
                  f"{domain} over the wire (HTTP 200) and the route sheet's "
                  f"block reads 'certificate live' with the numbers; the "
                  f"staging block reads the rehearsal issuer + 'no probe "
                  f"evidence yet'")
            liveness_check(c, oh, domain)
            print(f"[3] LIVENESS RIDING THE SURFACE OK - the work surface's "
                  f"liveness went from an honest null (never probed) to the "
                  f"green evidence (HTTP 200, the real milliseconds) the "
                  f"probe collected")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
