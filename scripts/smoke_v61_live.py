"""V61 live smoke: boot the real server and verify Py8n Systems.

1. Solution -> System: installing Invoice Processing with as_system
   creates a System binding the workflow + both datasets; the bound
   workflow RUNS offline and the system's derived health reflects it.
2. Manual system: bind a healthy workflow + a failing workflow + a
   dataset -> degraded verdict with the failing pipeline surfaced;
   attach guards (unknown kind, missing ref, duplicate).
3. Dissolve: members survive, system gone.

Usage: /home/z/.venv/bin/python scripts/smoke_v61_live.py
"""

from __future__ import annotations

import os
import subprocess
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
    db_path = f"{BACKEND}/data/smoke_v61_{uuid.uuid4().hex[:8]}.sqlite3"
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
    try:
        with httpx.Client(base_url=API, timeout=60) as c:
            wait_health(c)
            checks = 0

            # --- 1) solution -> system, run, health --------------------------
            res = c.post("/solutions/invoice-processing/install", json={"as_system": True})
            assert res.status_code in (200, 201), res.text
            out = res.json()
            assert out["system"], out
            sys_id = out["system"]["id"]
            detail = c.get(f"/systems/{sys_id}").json()
            assert detail["components"]["workflow"] == 1
            assert detail["components"]["dataset"] == 2
            wf_id = detail["grouped"]["workflow"][0]["ref_id"]
            res = c.post(f"/workflows/{wf_id}/run", json={})
            ex = res.json()["execution_id"]
            for _ in range(200):
                det = c.get(f"/executions/{ex}").json()
                if det["status"] not in ("running", "queued"):
                    break
                time.sleep(0.05)
            assert det["status"] == "success", det
            detail = c.get(f"/systems/{sys_id}").json()
            assert detail["health"]["workflows"]["runs_7d"] >= 1
            assert detail["health"]["verdict"] in ("healthy", "degraded")
            checks += 1
            print(f"[1] solution->system: {detail['name']} verdict={detail['health']['verdict']} "
                  f"runs={detail['health']['workflows']['runs_7d']}")

            # --- 2) manual system + guards -----------------------------------
            res = c.post("/systems", json={"name": "Ops Core", "description": "manual smoke system"})
            sys2 = res.json()["id"]

            res = c.post("/datasets", json={"name": "ops_core_ds", "rows": [{"a": 1}]})
            ds_id = res.json()["id"]
            graph = {"nodes": [
                {"id": "t1", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                {"id": "boom", "type": "code", "name": "boom", "position": {"x": 0, "y": 0}, "parameters": {"code": "result = 1 / 0"}},
            ], "edges": [{"id": "e1", "source": "t1", "target": "boom",
                          "sourceHandle": "main", "targetHandle": "main"}]}
            res = c.post("/workflows", json={"name": "ops_failing", "graph": graph})
            wf_bad = res.json()["id"]
            c.post(f"/workflows/{wf_bad}/run", json={})
            ex = ""
            res = c.post("/workflows", json={"name": "ops_ok", "graph": {
                "nodes": [
                    {"id": "t1", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "g", "type": "code", "name": "g", "position": {"x": 0, "y": 0}, "parameters": {"code": "result = 1"}},
                ],
                "edges": [{"id": "e1", "source": "t1", "target": "g", "sourceHandle": "main", "targetHandle": "main"}]}})
            wf_ok = res.json()["id"]

            for kind, ref in (("workflow", wf_ok), ("workflow", wf_bad), ("dataset", ds_id)):
                res = c.post(f"/systems/{sys2}/components", json={"kind": kind, "ref_id": ref})
                assert res.status_code == 201, res.text
            # guards
            assert c.post(f"/systems/{sys2}/components", json={"kind": "gadget", "ref_id": wf_ok}).status_code == 400
            assert c.post(f"/systems/{sys2}/components", json={"kind": "workflow", "ref_id": "missing"}).status_code == 404
            assert c.post(f"/systems/{sys2}/components", json={"kind": "workflow", "ref_id": wf_ok}).status_code == 409

            detail = c.get(f"/systems/{sys2}").json()
            assert detail["health"]["workflows"]["failures_7d"] >= 1
            assert detail["health"]["verdict"] == "degraded"
            failing_names = [w["name"] for w in detail["health"]["workflows"]["failing_workflows"]]
            assert "ops_failing" in failing_names
            checks += 1
            print(f"[2] manual system: verdict={detail['health']['verdict']} "
                  f"failing={failing_names} datasets={detail['health']['datasets']['total']}")

            # --- 3) detach + dissolve ------------------------------------------
            comp_id = detail["grouped"]["workflow"][0]["component_id"]
            assert c.delete(f"/systems/{sys2}/components/{comp_id}").status_code == 204
            assert c.get(f"/workflows/{wf_ok}").status_code == 200  # member survives unbind
            assert c.delete(f"/systems/{sys2}").status_code == 204
            assert c.get(f"/systems/{sys2}").status_code == 404
            assert c.get(f"/datasets/{ds_id}").status_code == 200  # members untouched
            checks += 1

            print(f"SMOKE v61 GREEN - {checks}/3 checks passed")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
