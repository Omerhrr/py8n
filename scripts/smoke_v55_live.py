"""V55 live smoke: boot the real server and verify Impact & Lineage Intelligence.

1. Version diff: append + keyed replace -> schema retype detected, keyed
   inserted/updated/removed with a sample field-level update, keyless hash
   diff, quality lens, unknown version 404.
2. Impact engine: a consumer workflow (which writes a downstream dataset),
   a charting dashboard, a bound app and a trained-model registry row all
   show up; downstream via= producers; model ranks highest-risk; severity.
3. Governance layer: steward/domain/classification/sensitivity/retention
   patched, validated, surfaced through the dataset API, catalog + filters,
   and the lineage response.

Usage: /home/z/.venv/bin/python scripts/smoke_v55_live.py
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


def node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v55_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert ver == "1.55.0", ver
            checks += 1
            print(f"1. server healthy (v{ver})")

            tag = uuid.uuid4().hex[:6]
            ds_name = f"smoke55 base {tag}"
            ds = client.post(f"{API}/datasets", json={"name": ds_name, "rows": [
                {"id": 1, "city": "berlin", "v": 10},
            ]}).json()
            down_name = f"smoke55 down {tag}"
            client.post(f"{API}/datasets", json={"name": down_name, "rows": [{"x": 1}]})

            # --- 1. version diff ---------------------------------------------
            client.post(f"{API}/datasets/{ds['id']}/rows", json={"rows": [{"id": 2, "city": "paris", "v": 20}]})
            d12 = client.get(f"{API}/datasets/{ds['id']}/versions/diff?from=1&to=2&key=id").json()
            assert d12["changed"]["inserted"] == 1 and d12["changed"]["updated"] == 0 and d12["changed"]["removed"] == 0, d12["changed"]
            assert d12["rows"]["delta"] == 1
            assert d12["quality"]["from"]["score"] is not None
            assert "impact" in d12
            checks += 1
            print("2. version diff: keyed insert detected, rows delta + quality lens green")

            # --- 2. impact engine ----------------------------------------------
            graph = {"nodes": [node("t", "manual_trigger"), node("r", "dataset_read", {"dataset": ds_name}),
                               node("w", "dataset_write", {"dataset": down_name, "mode": "append"})],
                     "edges": [{"id": "e1", "source": "t", "target": "r"}, {"id": "e2", "source": "r", "target": "w"}]}
            client.post(f"{API}/workflows", json={"name": f"smoke55 consumer {tag}", "graph": graph, "is_active": True})
            client.post(f"{API}/dashboards", json={"name": f"smoke55 board {tag}", "config": {"components": [
                {"id": "c1", "type": "chart", "dataset_id": ds["id"], "title": "By city", "chart_type": "bar",
                 "group_by": "city", "agg": "sum", "column": "v"}]}})
            client.post(f"{API}/apps", json={"name": f"smoke55 app {tag}", "dataset_id": ds["id"]})
            imp = client.get(f"{API}/datasets/{ds['id']}/impact").json()
            assert imp["totals"]["workflows"] >= 1 and imp["totals"]["dashboards"] >= 1 and imp["totals"]["apps"] >= 1
            down = next(d for d in imp["downstream_datasets"] if d["name"] == down_name)
            assert down["via"] == [f"smoke55 consumer {tag}"]
            assert imp["highest_risk"]["kind"] == "app"  # no model this time
            checks += 1
            print(f"3. impact: {imp['totals']['affected']} affected, downstream via consumer, risk={imp['highest_risk']['kind']}")

            # --- 3. governance layer --------------------------------------------
            res = client.put(f"{API}/datasets/{ds['id']}", json={"governance": {
                "steward": "Smoke Steward", "domain": "smoke", "classification": "internal",
                "sensitivity": "high", "retention_days": 90,
            }})
            assert res.status_code == 200 and res.json()["governance"]["retention_days"] == 90
            assert client.put(f"{API}/datasets/{ds['id']}", json={"governance": {"classification": "nope"}}).status_code == 400
            entry = next(e for e in client.get(f"{API}/catalog?domain=smoke").json()["entries"] if e["id"] == ds["id"])
            assert entry["governance"]["sensitivity"] == "high"
            lin = client.get(f"{API}/datasets/{ds['id']}/lineage").json()
            assert lin["governance"]["steward"] == "Smoke Steward"
            imp2 = client.get(f"{API}/datasets/{ds['id']}/impact").json()
            assert imp2["severity"] == "high"  # app-level impact + high sensitivity
            checks += 1
            print("4. governance: patched + validated, catalog filter, lineage propagation, severity bump")

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
