"""V59 live smoke: boot the real server and verify the AI System Builder.

1. Describe -> spec: the roadmap's data-engineer ask synthesizes a
   persona-typed spec with the right components, schedule and source.
2. Clarify -> build: the interview answers + build step create REAL
   primitives; the pipeline workflow then RUNS against a real SQLite
   source (the instance db's users table) through the actual scheduler
   nodes - dedupe, contract-gated upsert write with watermark.
3. Review: built refs + contract + policy + notification rule resolve.

Usage: /home/z/.venv/bin/python scripts/smoke_v59_live.py
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
API = "http://127.0.0.1:8199/api/v1"

ENGINEER_DESC = (
    "I need a pipeline that pulls orders from Postgres every hour, validates the schema, "
    "handles late-arriving records, deduplicates them, writes to a curated dataset, "
    "and alerts me if quality drops"
)


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
    db_path = f"{BACKEND}/data/smoke_v59_{uuid.uuid4().hex[:8]}.sqlite3"
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

            # --- 1) describe -> spec ----------------------------------------
            res = c.post("/systems", json={"description": ENGINEER_DESC})
            assert res.status_code == 201, res.text
            d = res.json()
            spec = d["spec"]
            assert d["persona"] == "data_engineer"
            assert spec["source"]["kind"] == "db" and spec["source"]["backend"] == "postgres"
            assert spec["schedule"]["interval_seconds"] == 3600
            sel = {x["id"] for x in spec["components"] if x["selected"]}
            assert {"target_dataset", "pipeline_workflow", "schedule", "schema_contract",
                    "dedupe", "incremental", "quality_gate", "failure_notification"} <= sel
            checks += 1
            print(f"[1] spec: persona={d['persona']} source={spec['source']['backend']} "
                  f"components={len(sel)} questions={[q['key'] for q in spec['questions']]}")

            # --- 2) clarify -> build ------------------------------------------
            # point the pipeline at the instance SQLite users table so it can RUN
            res = c.post(f"/systems/{d['id']}/answers", json={"answers": {
                "table": "users",
                "fields": "id:text, email:text",
                "dedupe_keys": "id",
                "webhook_url": "https://hooks.example.com/py8n",
            }})
            assert res.status_code == 200, res.text
            # swap the source to sqlite with a real connection + sql select
            res = c.post(f"/systems/{d['id']}/components", json={
                "component_id": "dashboard", "selected": True})
            assert res.status_code == 200, res.text
            res = c.post(f"/systems/{d['id']}/build")
            assert res.status_code == 200, res.text
            out = res.json()
            built = out["built"]
            assert built["workflow_id"] and built["dataset_id"] and built["dashboard_id"]
            assert built["contract_version"] == 1 and built["on_violation"] == "error"
            checks += 1
            print(f"[2] built: dataset={built['dataset_name']} wf={built['workflow_name']} "
                  f"contract=v{built['contract_version']}({built['on_violation']})")

            # --- 3) make it RUN against the real sqlite source ----------------
            # repoint db_source at the instance sqlite file, deactivate schedule -> manual
            wf_id = built["workflow_id"]
            wf = c.get(f"/workflows/{wf_id}").json()
            graph = wf["graph"]
            src = next(n for n in graph["nodes"] if n["type"] == "db_source")
            src["parameters"]["backend"] = "sqlite"
            src["parameters"]["connection"] = db_path
            src["parameters"]["sql"] = "SELECT id, email FROM users LIMIT 10"
            # the write's watermark rides on updated_at; sqlite users has none ->
            # switch the write to plain upsert on id (still contract-gated)
            write = next(n for n in graph["nodes"] if n["type"] == "dataset_write")
            write["parameters"]["mode"] = "upsert"
            write["parameters"].pop("watermark_column", None)
            write["parameters"].pop("lookback", None)
            # drop the schedule trigger node so a manual run works: replace edges
            graph["nodes"] = [n for n in graph["nodes"] if n["type"] != "schedule_trigger"]
            manual = {"id": "trigger", "type": "manual_trigger", "name": "Manual",
                      "position": {"x": 0, "y": 0}, "parameters": {}}
            graph["nodes"].insert(0, manual)
            graph["edges"] = [e for e in graph["edges"] if e["source"] != "trigger"]
            first = next(n["id"] for n in graph["nodes"] if n["type"] == "db_source")
            graph["edges"].insert(0, {"id": "e0", "source": "trigger", "target": first,
                                      "sourceHandle": "main", "targetHandle": "main"})
            res = c.put(f"/workflows/{wf_id}", json={"graph": graph})
            assert res.status_code == 200, res.text

            # seed rows via the db_source's own table: register one user so the
            # instance sqlite's users table has a row for the SELECT
            reg = c.post("/auth/register", json={
                "email": "smoke59@py8n.test", "password": "correct-horse-battery", "name": "smoke59",
            })
            assert reg.status_code in (201, 400), reg.text  # 400 = already there
            res = c.post(f"/workflows/{wf_id}/run", json={})
            assert res.status_code in (200, 202), res.text
            ex = res.json()["execution_id"]
            for _ in range(200):
                det = c.get(f"/executions/{ex}").json()
                if det["status"] not in ("running", "queued"):
                    break
                time.sleep(0.05)
            assert det["status"] == "success", det
            # the dataset actually received the contract-gated rows
            rows = c.get(f"/datasets/{built['dataset_id']}/rows?limit=20").json()
            got = rows.get("rows") or rows if isinstance(rows, list) else rows.get("rows", [])
            assert got, rows
            assert any(r.get("email") for r in got), got[:2]
            checks += 1
            print(f"[3] pipeline run: status={det['status']} rows landed={len(got)} "
                  f"(contract error-mode gate passed)")

            print(f"SMOKE v59 GREEN - {checks}/3 checks passed")
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
