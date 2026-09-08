"""V103 live smoke: boot the real server and walk the round on the real
wire - the branded front door and the deployment liveness.

1. THE LIVENESS PING: a system gets a deployment (domain + production),
   DEPLOYS, and then the platform ASKS THE DOMAIN IF IT ANSWERS. The
   probe target rides the dev override (PY8N_DEPLOY_PING_URL) pointing
   at the smoke server's own /health - the domain "answers" with HTTP
   200 and a real round-trip time, the evidence lands on the record
   (last_ping_*), the operations log names deployment_ping, and a
   deployment with no domain refuses loud (400).
2. THE FRONT DOOR + THE ESTATE: the PUBLIC by-domain door resolves the
   live deployment back to the identity a landing page renders (name,
   branding, environment, my_role), the estate health row wears the
   compact ping evidence - and pausing turns BOTH dark: the door 404s
   and the surface stops presenting itself.

Usage: /home/z/.venv/bin/python scripts/smoke_v103_live.py
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
# 1) the liveness probe over the wire
# ---------------------------------------------------------------------------

def liveness_check(c: httpx.Client) -> str:
    r = c.post("/systems", json={"name": "Acme Ops v103", "color": "#38bdf8",
                                 "description": "The Acme revenue line"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    # ping with no deployment record refuses loud
    r = c.post(f"/systems/{sid}/deployment/ping")
    assert r.status_code == 400 and "no custom domain" in r.json()["detail"], r.text

    # the identity saves, DEPLOY goes live
    r = c.put(f"/systems/{sid}/deployment",
              json={"domain": f"ops{uuid.uuid4().hex[:6]}.acme.com",
                    "environment": "production",
                    "branding": {"accent": "#38bdf8", "tagline": "The Acme revenue line",
                                 "login_headline": "Acme Operations"}})
    assert r.status_code == 200, r.text
    r = c.post(f"/systems/{sid}/deployment/deploy")
    assert r.status_code == 200 and r.json()["deployment"]["status"] == "live", r.text

    # THE PING: the probe target is the override (this very server's
    # /health) - the domain answers 200 with a REAL round-trip time
    r = c.post(f"/systems/{sid}/deployment/ping")
    assert r.status_code == 200, r.text
    ping = r.json()["ping"]
    assert ping["ok"] is True and ping["code"] == 200, ping
    assert isinstance(ping["ms"], int) and ping["ms"] >= 0, ping
    dep = r.json()["deployment"]
    assert dep["last_ping"]["ok"] is True and dep["last_ping"]["at"], dep

    # the evidence is on the record
    r = c.get(f"/systems/{sid}/operations")
    pings = [o for o in r.json()["operations"] if o["verb"] == "deployment_ping"]
    assert pings and pings[0]["detail"]["ok"] is True, pings
    return sid


# ---------------------------------------------------------------------------
# 2) the front door + the estate wearing the evidence
# ---------------------------------------------------------------------------

def door_and_estate_check(c: httpx.Client, sid: str) -> None:
    # the PUBLIC door: the identity a landing page renders
    dep = c.get(f"/systems/{sid}/deployment").json()["deployment"]
    r = c.get(f"/systems/by-domain/{dep['domain']}")
    assert r.status_code == 200, r.text
    ident = r.json()
    assert ident["system"]["name"] == "Acme Ops v103", ident
    assert ident["branding"]["accent"] == "#38bdf8", ident
    assert ident["branding"]["login_headline"] == "Acme Operations", ident
    assert ident["environment"] == "production" and ident["status"] == "live", ident
    assert ident["my_role"] == "owner", ident  # auth-off: the operator's own door

    # the estate health row wears the compact ping evidence
    r = c.get("/systems/health/overview")
    rows = {row["name"]: row for row in r.json()["systems"]}
    assert "Acme Ops v103" in rows, list(rows)
    riding = rows["Acme Ops v103"]["deployment"]
    assert riding["last_ping"] and riding["last_ping"]["ok"] is True, riding
    assert riding["status"] == "live", riding

    # pausing turns the door dark - an honest 404, the surface is gone
    r = c.post(f"/systems/{sid}/deployment/pause")
    assert r.status_code == 200, r.text
    r = c.get(f"/systems/by-domain/{dep['domain']}")
    assert r.status_code == 404 and "not live" in r.json()["detail"], r.text
    r = c.post(f"/systems/{sid}/deployment/retire")
    assert r.status_code == 200 and r.json()["deployment"]["status"] == "offline", r.text


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v103_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the door
        "PY8N_DEPLOY_PING_OVERRIDE": f"http://127.0.0.1:{SERVER_PORT}/api/v1/health",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v103_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.103.0", version

            sid = liveness_check(c)
            print(f"[1] LIVENESS PING OK - the no-domain refusal was loud (400), "
                  f"the probe asked the deployment's domain and it ANSWERED "
                  f"(HTTP 200, a real round-trip time), the evidence landed on "
                  f"the record (last_ping_*) and in the operations log")
            door_and_estate_check(c, sid)
            print(f"[2] FRONT DOOR + ESTATE OK - the PUBLIC by-domain door resolved "
                  f"the live deployment back to the identity a landing page renders "
                  f"(name, branding, environment, my_role), the estate health row "
                  f"wore the compact ping evidence, and pausing turned the door "
                  f"dark (honest 404)")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
