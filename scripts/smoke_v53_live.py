"""V53 live smoke: boot the real server and verify the three feature areas E2E.

1. Observability surface: /observability/events stitches dataset writes,
   workflow outcomes, report deliveries and denied share attempts into one
   derived stream (type + severity filters work); /observability/overview
   composes fleet-wide health, pipeline reliability, ingestion and deliveries.
2. Contract editor backend parity on the catalog: contract PUT + check
   against CURRENT data through the same endpoints the new modal drives.
3. Incremental deepening: dataset_write mode=upsert + watermark_column +
   lookback merges late-arriving corrections on key (no duplicates), and
   the ingestion-states API carries per-run stats.

Usage: /home/z/.venv/bin/python scripts/smoke_v53_live.py
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


def run_workflow(client: httpx.Client, wf_id: str, payload: dict | None = None) -> dict:
    res = client.post(f"{API}/workflows/{wf_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(200):
        body = client.get(f"{API}/executions/{exec_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.05)
    raise SystemExit("execution did not finish")


def node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v53_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert ver == "1.53.0", ver
            checks += 1
            print(f"1. server healthy (v{ver})")

            tag = uuid.uuid4().hex[:6]

            # --- 1. observability: produce real signal, then read the stream --
            res = client.post(f"{API}/datasets", json={
                "name": f"smoke53 obs {tag}",
                "rows": [{"city": "berlin", "ts": "10"}, {"city": "paris", "ts": "20"}],
            })
            assert res.status_code == 201, res.text
            ds_id = res.json()["id"]

            ok_graph = {"nodes": [node("t", "manual_trigger"), node("s", "set_variable", {"assignments": {"x": "1"}})],
                        "edges": [{"id": "e1", "source": "t", "target": "s", "sourceHandle": "main", "targetHandle": "main"}]}
            ok_wf = client.post(f"{API}/workflows", json={"name": f"smoke53 ok {tag}", "graph": ok_graph, "is_active": False}).json()["id"]
            assert run_workflow(client, ok_wf)["status"] == "success"

            bad_graph = {"nodes": [node("t", "manual_trigger"), node("c", "code", {"code": "result = 1 / 0"})],
                         "edges": [{"id": "e1", "source": "t", "target": "c", "sourceHandle": "main", "targetHandle": "main"}]}
            bad_wf = client.post(f"{API}/workflows", json={"name": f"smoke53 bad {tag}", "graph": bad_graph, "is_active": False}).json()["id"]
            assert run_workflow(client, bad_wf)["status"] == "error"

            ev = client.get(f"{API}/observability/events").json()
            types = {e["type"] for e in ev["events"]}
            assert {"dataset.written", "workflow.succeeded", "workflow.failed"} <= types, types
            failed = next(e for e in ev["events"] if e["type"] == "workflow.failed")
            assert failed["severity"] == "error" and "division by zero" in (failed["detail"] or "")
            only_ok = client.get(f"{API}/observability/events?type=workflow.succeeded").json()
            assert {e["type"] for e in only_ok["events"]} == {"workflow.succeeded"}
            only_err = client.get(f"{API}/observability/events?severity=error").json()
            assert {e["severity"] for e in only_err["events"]} == {"error"}
            checks += 1
            print(f"2. event stream: {len(ev['events'])} events stitched, type+severity filters green")

            ov = client.get(f"{API}/observability/overview").json()
            assert ov["datasets"]["total"] >= 1 and ov["pipelines"]["runs_24h"] >= 2
            assert ov["pipelines"]["failures_7d"] >= 1 and ov["pipelines"]["failing_workflows"]
            assert ov["incidents"] and ov["incidents"][0]["type"] == "workflow.failed"
            checks += 1
            print(f"3. overview: overall={ov['overall']}, {ov['datasets']['total']} datasets, {len(ov['incidents'])} incident(s)")

            # --- 2. contract endpoints the catalog editor drives ---------------
            res = client.put(f"{API}/datasets/{ds_id}/contract", json={
                "on_violation": "error",
                "columns": [{"name": "city", "dtype": "text", "nullable": False, "allowed": None},
                            {"name": "ts", "dtype": "integer", "nullable": False, "allowed": None}],
            })
            assert res.status_code == 200 and res.json()["version"] == 1, res.text
            check = client.post(f"{API}/datasets/{ds_id}/contract/check", json={"rows": []}).json()
            assert check["ok"] is True and check["checked_rows"] == 2, check
            res = client.post(f"{API}/datasets/{ds_id}/rows", json={"rows": [{"city": "rome", "ts": "not-a-number"}]})
            assert res.status_code == 422, res.text  # error-mode hard stop
            checks += 1
            print("4. contract editor backend: put + lint-current-data + 422 hard stop green")

            # --- 3. incremental upsert with watermark + lookback ---------------
            ds_name = f"smoke53 upsert {tag}"
            graph = {"nodes": [node("t", "manual_trigger"),
                               node("w", "dataset_write", {"dataset": ds_name, "mode": "upsert",
                                                           "key_columns": ["id"], "watermark_column": "ts",
                                                           "ingestion_key": "pipe1", "lookback": 15})],
                     "edges": [{"id": "e1", "source": "t", "target": "w", "sourceHandle": "main", "targetHandle": "main"}]}
            wf = client.post(f"{API}/workflows", json={"name": f"smoke53 upsert {tag}", "graph": graph, "is_active": False}).json()["id"]

            r1 = run_workflow(client, wf, {"items": [{"id": "1", "city": "berlin", "ts": "10"}, {"id": "2", "city": "paris", "ts": "20"}]})
            out1 = next(n for n in r1["node_runs"] if n["node_name"] == "w")["output"]
            assert out1["written"] == 2 and out1["inserted"] == 2, out1

            # correction inside the lookback window merges; new key inserts
            r2 = run_workflow(client, wf, {"items": [{"id": "2", "city": "paris-final", "ts": "26"}, {"id": "3", "city": "rome", "ts": "40"}]})
            out2 = next(n for n in r2["node_runs"] if n["node_name"] == "w")["output"]
            assert out2["updated"] == 1 and out2["inserted"] == 1, out2

            rows_res = client.get(f"{API}/datasets").json()
            ds2 = next(d for d in rows_res if d["name"] == ds_name)
            rows = client.get(f"{API}/datasets/{ds2['id']}/rows").json()
            by_id = {r["id"]: r["city"] for r in rows["rows"]}
            assert by_id == {"1": "berlin", "2": "paris-final", "3": "rome"}, by_id
            assert rows["row_count"] == 3  # no duplicates

            states = {s["key"]: s for s in client.get(f"{API}/datasets/{ds2['id']}/ingestion-states").json()}
            st = states["pipe1"]
            assert st["watermark"] == "40"
            assert st["stats"]["mode"] == "upsert" and st["stats"]["updated"] == 1 and st["stats"]["lookback"] == 15
            checks += 1
            print("5. incremental upsert: late correction merged on key (3 rows, no dupes), stats recorded")

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
