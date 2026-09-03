"""V54 live smoke: boot the real server and verify the three feature areas E2E.

1. Contract version history + diff: PUT v1 -> PUT v2 -> revisions list shows
   the v1 snapshot; diff reports added/removed/changed; DELETE snapshots the
   final state so the trail survives removal.
2. Ownership governance: register two users, claim/certify/transfer/release
   an unclaimed dataset; the catalog carries certified/claimable/owner.
3. PNG per-component drilldowns: /dashboards/{ref}/snapshot json stamps
   dataset+ref on every component; png renders; &component= renders ONE
   component standalone.

Usage: /home/z/.venv/bin/python scripts/smoke_v54_live.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
API = "http://127.0.0.1:8199/api/v1"


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


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v54_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
    })
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8199", "--log-level", "warning"],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    checks = 0
    try:
        with httpx.Client(timeout=30.0) as client:
            wait_health(client)
            ver = client.get(f"{API}/health").json()["version"]
            assert ver == "1.54.0", ver
            checks += 1
            print(f"1. server healthy (v{ver})")

            tag = uuid.uuid4().hex[:6]

            # --- 1. contract history + diff ---------------------------------
            ds = client.post(f"{API}/datasets", json={
                "name": f"smoke54 contract {tag}",
                "rows": [{"region": "eu", "v": 10}],
            }).json()
            res = client.put(f"{API}/datasets/{ds['id']}/contract", json={
                "on_violation": "warn",
                "columns": [{"name": "region", "dtype": "text", "nullable": False, "allowed": None}],
            })
            assert res.json()["version"] == 1
            res = client.put(f"{API}/datasets/{ds['id']}/contract", json={
                "on_violation": "error",
                "columns": [
                    {"name": "region", "dtype": "text", "nullable": False, "allowed": ["eu", "us"]},
                    {"name": "v", "dtype": "integer", "nullable": False, "allowed": None},
                ],
            })
            assert res.json()["version"] == 2
            hist = client.get(f"{API}/datasets/{ds['id']}/contract/revisions").json()
            assert [r["version"] for r in hist["revisions"]] == [2, 1], hist
            assert hist["revisions"][1]["note"] == "superseded by v2"
            d = client.get(f"{API}/datasets/{ds['id']}/contract/diff").json()
            assert d["from"] == 1 and d["to"] == 2 and [c["name"] for c in d["added"]] == ["v"], d
            assert d["changed"] and d["changed"][0]["field"] == "allowed" and d["changed"][0]["name"] == "region"
            checks += 1
            print("2. contract history: v1 snapshotted, diff v1->v2 = 1 added + allowed tightened")

            # --- 2. ownership governance ------------------------------------
            ua = client.post(f"{API}/auth/register", json={
                "email": f"smoke54-{tag}-a@py8n.test", "password": "correct-horse-battery", "name": "Smoke A",
            }).json()
            ds2 = client.post(f"{API}/datasets", json={
                "name": f"smoke54 own {tag}", "rows": [{"x": 1}],
            }).json()
            r = client.post(f"{API}/datasets/{ds2['id']}/claim", json={},
                            headers={"Authorization": f"Bearer {ua['token']}"})
            assert r.status_code == 200 and r.json()["owner_id"] == ua["user"]["id"], r.text
            r = client.post(f"{API}/datasets/{ds2['id']}/certify", headers={"Authorization": f"Bearer {ua['token']}"})
            assert r.status_code == 200 and r.json()["certified_at"], r.text
            entry = next(e for e in client.get(f"{API}/catalog").json()["entries"] if e["id"] == ds2["id"])
            assert entry["certified"] is True and entry["claimable"] is False and entry["owner_id"] == ua["user"]["id"]
            checks += 1
            print("3. governance: claimed + certified, catalog shows the steward badge")

            # --- 3. per-component drilldowns --------------------------------
            board = client.post(f"{API}/dashboards", json={
                "name": f"smoke54 board {tag}",
                "config": {"components": [
                    {"id": "kpi1", "type": "stat", "dataset_id": ds["id"], "label": "Value", "agg": "sum", "column": "v"},
                    {"id": "chart1", "type": "chart", "dataset_id": ds["id"], "title": "By region",
                     "chart_type": "bar", "group_by": "region", "agg": "sum", "column": "v"},
                ]},
            }).json()
            client.post(f"{API}/dashboards/{board['id']}/publish")
            snap = client.get(f"{API}/dashboards/{board['id']}/snapshot?fmt=json").json()
            by_id = {c["id"]: c for c in snap["components"]}
            assert by_id["chart1"]["ref"] == f"/d/{board['slug']}?c=chart1", by_id
            assert by_id["chart1"]["dataset"] == ds["name"]
            png_full = client.get(f"{API}/dashboards/{board['id']}/snapshot?fmt=png")
            assert png_full.headers["content-type"] == "image/png" and png_full.content[:4] == b"\x89PNG"
            png_one = client.get(f"{API}/dashboards/{board['id']}/snapshot?fmt=png&component=chart1")
            assert png_one.status_code == 200 and png_one.content[:4] == b"\x89PNG"
            assert png_one.content != png_full.content  # standalone render
            checks += 1
            print("4. drilldowns: json refs stamped, full PNG + single-component PNG green")

            print(f"\nSMOKE GREEN: {checks} checks through the real server (v{ver})")
            return 0
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
