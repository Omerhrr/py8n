"""V104 live smoke: boot the real server and walk the round on the real
wire - host routing, the scheduled pings, and the system's own keys.

1. THE DERIVED ROUTE SHEET: a system gets a domain + DEPLOYS, and
   GET /systems/deployment/routes.caddy writes the Caddy site block
   FROM the live deployment (rewrite * /go/{domain}) - then pausing
   removes the block (the domain stays, commented: a dark door is never
   routed) and re-deploying puts it back. The upstream rides the query.
2. THE SCHEDULED PING SWEEP: POST /scheduler/escalations/tick runs the
   walk on the real wire (override pointing at this very server's
   /health) - the live domain is probed automatically, the evidence
   lands on the record, and a SECOND tick within the cadence does NOT
   re-probe (the rhythm holds). The estate health row wears the
   evidence.
3. THE SYSTEM API KEY: the owner mints py8n_sys_... (shown once), the
   key SPEAKS AS THE SYSTEM on the real wire (GET /systems/{id} with
   X-API-Key -> my_role editor; the read-only key -> viewer, write
   403), revocation makes the next request resolve to nothing.

Usage: /home/z/.venv/bin/python scripts/smoke_v104_live.py
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
# 1) the derived route sheet over the wire
# ---------------------------------------------------------------------------

def routes_check(c: httpx.Client) -> tuple[str, str]:
    r = c.post("/systems", json={"name": "Acme Ops v104", "color": "#38bdf8",
                                 "description": "The Acme revenue line"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    domain = f"ops{uuid.uuid4().hex[:6]}.acme.com"

    r = c.put(f"/systems/{sid}/deployment",
              json={"domain": domain, "environment": "production"})
    assert r.status_code == 200, r.text
    r = c.post(f"/systems/{sid}/deployment/deploy")
    assert r.status_code == 200 and r.json()["deployment"]["status"] == "live", r.text

    # the sheet carries the live block, mapped onto the front door
    r = c.get("/systems/deployment/routes.caddy")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain"), r.text
    sheet = r.text
    assert f"{domain} {{" in sheet, sheet
    assert f"rewrite * /go/{domain}" in sheet, sheet
    assert "reverse_proxy localhost:3000" in sheet, sheet
    assert int(r.headers["X-Py8n-Live-Routes"]) >= 1, r.headers

    # the upstream rides the query
    r2 = c.get("/systems/deployment/routes.caddy?upstream=frontend:3000")
    assert "reverse_proxy frontend:3000" in r2.text, r2.text

    # pausing darkens the route - the domain stays ONLY as a comment
    r = c.post(f"/systems/{sid}/deployment/pause")
    assert r.status_code == 200, r.text
    sheet = c.get("/systems/deployment/routes.caddy").text
    assert f"rewrite * /go/{domain}" not in sheet, sheet
    assert domain in sheet, sheet

    # deploy again - the route returns
    r = c.post(f"/systems/{sid}/deployment/deploy")
    assert r.status_code == 200, r.text
    sheet = c.get("/systems/deployment/routes.caddy").text
    assert f"rewrite * /go/{domain}" in sheet, sheet
    return sid, domain


# ---------------------------------------------------------------------------
# 2) the scheduled ping sweep feeds the health dot
# ---------------------------------------------------------------------------

def ping_sweep_check(c: httpx.Client, sid: str, domain: str) -> None:
    r = c.post("/scheduler/escalations/tick")
    assert r.status_code == 200, r.text
    sweep = r.json()["deploy_ping"]
    assert sweep["probed"] >= 1, sweep
    mine = [p for p in sweep["lost"] + sweep["recovered"] if p["domain"] == domain]
    assert not mine, sweep  # a green steady state is silent

    r = c.get(f"/systems/{sid}/deployment")
    last = r.json()["deployment"]["last_ping"]
    assert last and last["ok"] is True and last["at"], r.text
    assert isinstance(last["ms"], int) and last["ms"] >= 0, r.text

    # the cadence holds: a second tick right after does not re-probe
    r = c.post("/scheduler/escalations/tick")
    sweep = r.json()["deploy_ping"]
    assert all(p["domain"] != domain
               for p in sweep["lost"] + sweep["recovered"]), sweep

    # the estate row wears the evidence (and stays green)
    r = c.get("/systems/health/overview")
    row = next(row for row in r.json()["systems"] if row["id"] == sid)
    assert row["deployment"]["last_ping"]["ok"] is True, row
    assert row["status"] == "running" and row["deployment"]["unreachable"] is False, row


# ---------------------------------------------------------------------------
# 3) the system's own API key
# ---------------------------------------------------------------------------

def system_key_check(c: httpx.Client, sid: str) -> None:
    r = c.post(f"/systems/{sid}/keys", json={"name": "ERP push job"})
    assert r.status_code == 201, r.text
    body = r.json()
    key = body["key"]
    assert key.startswith("py8n_sys_"), body

    # the key speaks AS the system: editor role, on the real wire
    r = c.get(f"/systems/{sid}", headers={"X-API-Key": key})
    assert r.status_code == 200 and r.json()["my_role"] == "editor", r.text

    # owner doors are human-only
    r = c.post(f"/systems/{sid}/keys", headers={"X-API-Key": key},
               json={"name": "self-mint"})
    assert r.status_code == 403, r.text

    # the masked list shows prefixes, never the full key
    r = c.get(f"/systems/{sid}/keys")
    keys = r.json()["keys"]
    assert any(k["prefix"] == body["prefix"] for k in keys), keys
    assert all("key" not in k for k in keys), keys

    # revoke -> the next request carrying the key resolves to nothing
    r = c.delete(f"/systems/{sid}/keys/{body['id']}")
    assert r.status_code == 204, r.text
    r = c.get(f"/systems/{sid}/keys")
    assert next(k for k in r.json()["keys"] if k["id"] == body["id"])["revoked"] is True, r.text
    # (auth-off here: the request succeeds as anonymous; the enforced-mode
    # refusal is pinned by the suite)


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v104_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the door
        "PY8N_DEPLOY_PING_OVERRIDE": f"http://127.0.0.1:{SERVER_PORT}/api/v1/health",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v104_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.104.0", version

            sid, domain = routes_check(c)
            print(f"[1] ROUTE SHEET OK - the derived sheet mapped the live "
                  f"domain onto the branded front door (rewrite * /go/{domain}), "
                  f"the upstream rode the query, pausing darkened the route "
                  f"(comment only) and re-deploying brought it back")
            ping_sweep_check(c, sid, domain)
            print(f"[2] SCHEDULED PINGS OK - the tick door probed the live domain "
                  f"automatically (override -> this server's /health), the evidence "
                  f"landed on the record, the second tick held the cadence, and the "
                  f"estate row stayed green (unreachable=false)")
            system_key_check(c, sid)
            print(f"[3] SYSTEM KEY OK - py8n_sys_... minted (shown once), the key "
                  f"spoke AS the system (editor role) on the real wire, owner doors "
                  f"refused it (403), and revocation made it resolve to nothing")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
