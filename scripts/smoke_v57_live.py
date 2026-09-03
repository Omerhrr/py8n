"""V57 live smoke: boot the real server and verify the Operations Center.

1. Ops overview: SYSTEM verdict + workflows/datasets/reports/agents rollup
   after seeding a healthy run, a failed run and an agent workflow.
2. Incident drilldown: the full chain walks workflow -> execution ->
   failed node -> input -> error -> previous success -> related dataset
   (health) -> impact.

Usage: /home/z/.venv/bin/python scripts/smoke_v57_live.py
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


def node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v57_{uuid.uuid4().hex[:8]}.sqlite3"
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

            # seed: dataset + a workflow that succeeds, then breaks
            res = c.post("/datasets", json={"name": "ops_smoke_ds", "rows": [{"a": 1}]})
            assert res.status_code == 201, res.text
            ds = res.json()

            graph = {"nodes": [
                node("t1", "manual_trigger"),
                node("calc", "code", {"code": "result = {'x': 1}"}),
                node("save", "dataset_write", {"dataset": ds["name"], "rows": "{{ nodes.calc.output }}"}),
            ], "edges": [
                {"id": "e1", "source": "t1", "target": "calc", "sourceHandle": "main", "targetHandle": "main"},
                {"id": "e2", "source": "calc", "target": "save", "sourceHandle": "main", "targetHandle": "main"},
            ]}

            def run_wf(wid: str) -> dict:
                res = c.post(f"/workflows/{wid}/run", json={})
                assert res.status_code in (200, 202), res.text
                ex = res.json()["execution_id"]
                for _ in range(200):
                    det = c.get(f"/executions/{ex}").json()
                    if det["status"] not in ("running", "queued"):
                        break
                    time.sleep(0.05)
                return det

            res = c.post("/workflows", json={"name": "ops_smoke_wf", "graph": graph})
            assert res.status_code == 201, res.text
            wf = res.json()["id"]
            assert run_wf(wf)["status"] == "success"

            graph["nodes"][1]["parameters"]["code"] = "result = 1 / 0"
            res = c.put(f"/workflows/{wf}", json={"graph": graph})
            assert res.status_code == 200, res.text
            bad = run_wf(wf)
            assert bad["status"] == "error"

            # agent workflow for the agents card
            agent_graph = {"nodes": [node("t1", "manual_trigger"),
                                     node("ai", "llm_chat", {"user_prompt": "hi"})],
                           "edges": [{"id": "e1", "source": "t1", "target": "ai",
                                      "sourceHandle": "main", "targetHandle": "main"}]}
            res = c.post("/workflows", json={"name": "ops_smoke_agent", "graph": agent_graph})
            assert res.status_code == 201, res.text

            # --- 1) ops overview --------------------------------------------
            ops = c.get("/ops/overview").json()
            assert ops["verdict"] == "degraded", ops["verdict"]
            assert ops["workflows"]["total"] >= 2 and ops["workflows"]["failed_24h"] >= 1  # seeded demos ride along
            assert ops["workflows"]["runs_24h"] >= 2
            assert ops["datasets"]["total"] >= 1
            assert ops["agents"]["agent_workflows"] >= 1  # the seed demo also carries an llm_chat node
            failed_incidents = [e for e in ops["incidents"] if e["type"] == "workflow.failed"]
            assert failed_incidents, ops["incidents"]
            checks += 1
            print(f"[1] ops overview: verdict={ops['verdict']} workflows={ops['workflows']['total']} "
                  f"agents={ops['agents']['agent_workflows']} incidents={len(ops['incidents'])}")

            # --- 2) incident drilldown --------------------------------------
            exec_id = failed_incidents[0]["meta"]["execution_id"]
            chain = c.get(f"/ops/incidents/{exec_id}").json()
            steps = [s["step"] for s in chain["chain"]]
            assert steps == ["workflow", "execution", "node", "input", "error",
                             "previous_success", "related_datasets", "impact"], steps
            assert chain["failed_node"]["node_id"] == "calc"
            assert "ZeroDivision" in str(chain["failed_node"]["error"])
            comp = chain["comparison_with_previous_success"]
            assert comp and comp["node"]["previous_status"] == "success"
            assert any(d["name"] == "ops_smoke_ds" for d in chain["related_datasets"])
            assert chain["related_datasets"][0]["health"] is not None
            assert chain["impact"], "impact must ride along"
            checks += 1
            print(f"[2] drilldown: node={chain['failed_node']['node_name']} "
                  f"severity={chain['severity']} datasets={[d['name'] for d in chain['related_datasets']]}")

            # --- 3) unknown execution 404 ------------------------------------
            res = c.get("/ops/incidents/nope")
            assert res.status_code == 404
            checks += 1

            print(f"SMOKE v57 GREEN - {checks}/3 checks passed")
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
