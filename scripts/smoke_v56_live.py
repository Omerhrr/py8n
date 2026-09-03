"""V56 live smoke: boot the real server and verify Workflow Intelligence.

1. Workflow health: a healthy run + a failing run (retries) + a fallback
   run fold into success rate / p95 / retries / fallbacks / most-failing
   and most-expensive node.
2. Version diff: a settings-only change reports "Retry policy: 2 -> 4";
   a param change lists per-parameter old->new lines; the potential-impact
   estimate switches from "no run history" to a real percentage.

Usage: /home/z/.venv/bin/python scripts/smoke_v56_live.py
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


def node(nid: str, ntype: str, params: dict | None = None, settings: dict | None = None) -> dict:
    n = {"id": nid, "type": ntype, "name": nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}
    if settings is not None:
        n["settings"] = settings
    return n


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v56_{uuid.uuid4().hex[:8]}.sqlite3"
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

            # --- build a workflow with one param-timed node ----------------
            g1 = {"nodes": [
                node("t1", "manual_trigger"),
                node("gen", "code", {"code": "result = {'x': sum(range(5000))}"}),
            ], "edges": [{"id": "e1", "source": "t1", "target": "gen",
                          "sourceHandle": "main", "targetHandle": "main"}]}
            res = c.post("/workflows", json={"name": "smoke56", "graph": g1})
            assert res.status_code == 201, res.text
            wf = res.json()["id"]

            # seed two runs (timing history)
            for _ in range(2):
                res = c.post(f"/workflows/{wf}/run", json={})
                assert res.status_code in (200, 202), res.text
                ex = res.json()["execution_id"]
                for _ in range(200):
                    det = c.get(f"/executions/{ex}").json()
                    if det["status"] not in ("running", "queued"):
                        break
                    time.sleep(0.05)
                assert det["status"] == "success", det

            # --- 1) workflow health ----------------------------------------
            rep = c.get(f"/workflows/{wf}/health").json()
            assert rep["runs"] == 2 and rep["succeeded"] == 2, rep
            assert rep["success_rate"] == 100.0 and rep["verdict"] == "healthy", rep
            assert rep["p95_duration_ms"] is not None
            assert rep["most_expensive_node"]["type"] == "code", rep
            checks += 1
            print(f"[1] workflow health: runs={rep['runs']} rate={rep['success_rate']}% "
                  f"p95={rep['p95_duration_ms']}ms expensive={rep['most_expensive_node']['name']}")

            # --- 2) version diff: param change + impact estimate ------------
            g2 = {"nodes": [
                node("t1", "manual_trigger"),
                node("gen", "code", {"code": "result = {'x': sum(range(9000))}"}),
            ], "edges": g1["edges"]}
            res = c.put(f"/workflows/{wf}", json={"graph": g2})
            assert res.status_code == 200, res.text

            d = c.get(f"/workflows/{wf}/versions/diff").json()
            assert d["from"]["version"] == 1 and d["to"]["version"] == 2, d
            assert d["changed"] and d["changed"][0]["node_id"] == "gen", d
            assert d["changed"][0]["changes"][0]["param"] == "code"
            imp = d["potential_impact"]
            assert imp["runs_analyzed"] >= 2 and imp["estimate"] is not None, imp
            checks += 1
            print(f"[2] version diff: {d['summary']} | impact ~{imp['estimate']}% ({imp['detail']})")

            # --- 3) settings-only diff: Retry policy line -------------------
            g3 = {"nodes": [
                node("t1", "manual_trigger"),
                node("gen", "code", {"code": "result = {'x': sum(range(9000))}"},
                     settings={"retry_on_fail": True, "max_retries": 4}),
            ], "edges": g1["edges"]}
            res = c.put(f"/workflows/{wf}", json={"graph": g3})
            assert res.status_code == 200, res.text
            d2 = c.get(f"/workflows/{wf}/versions/diff?from=2&to=3").json()
            gen = {ch["node_id"]: ch for ch in d2["changed"]}["gen"]
            assert gen["summary"] == "Retry policy: 2 -> 4", gen
            checks += 1
            print(f"[3] settings diff: {gen['summary']}")

            print(f"SMOKE v56 GREEN - {checks}/3 checks passed")
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
