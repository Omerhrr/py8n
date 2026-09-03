"""V51 live smoke: boot the real server and verify the three features E2E.

1. GET /storage reports the local backend with a passing ping.
2. A workflow-level policy retries a transient failure through the real
   executor and records the policy on the node run.
3. Dashboard share audit: allowed + denied renders land in the trail.

Usage: /home/z/.venv/bin/python scripts/smoke_v51_live.py
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
    db_path = f"{BACKEND}/data/smoke_v51_{uuid.uuid4().hex[:8]}.sqlite3"
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
        with httpx.Client(timeout=20.0) as client:
            wait_health(client)
            checks += 1
            print("1. server healthy")

            # --- 1. storage status -------------------------------------------------
            res = client.get(f"{API}/storage")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["kind"] == "local" and body["ping"] is True, body
            checks += 1
            print(f"2. storage status: {body}")

            # --- 2. workflow policy E2E -------------------------------------------
            wf = client.post(f"{API}/workflows", json={
                "name": "smoke v51 policy",
                "graph": {
                    "nodes": [
                        {"id": "t1", "type": "manual_trigger", "name": "t1", "position": {"x": 0, "y": 0}, "parameters": {}},
                        {"id": "c1", "type": "code", "name": "victim", "position": {"x": 1, "y": 0},
                         "parameters": {"code": "timeout_variable"}},
                    ],
                    "edges": [{"id": "e1", "source": "t1", "target": "c1",
                               "sourceHandle": "main", "targetHandle": "main"}],
                },
                "policy": {"retries": 2, "backoff_ms": 20, "backoff_multiplier": 2, "retry_on": "transient"},
            }).json()
            run = client.post(f"{API}/workflows/{wf['id']}/run", json={"payload": {}}).json()
            for _ in range(100):
                ex = client.get(f"{API}/executions/{run['execution_id']}").json()
                if ex["status"] != "running":
                    break
                time.sleep(0.1)
            victim = next(r for r in ex["node_runs"] if r["node_name"] == "victim")
            assert victim["attempts"] == 3, victim
            assert victim["policy"]["source"] == "workflow", victim
            assert "timeout_variable" in (victim["error"] or "")
            checks += 1
            print(f"3. policy retry E2E: attempts={victim['attempts']} backoff=20->40ms source=workflow")

            # permanent failure fails fast under the same policy
            wf2 = client.post(f"{API}/workflows", json={
                "name": "smoke v51 policy permanent",
                "graph": {
                    "nodes": [
                        {"id": "t1", "type": "manual_trigger", "name": "t1", "position": {"x": 0, "y": 0}, "parameters": {}},
                        {"id": "c1", "type": "code", "name": "victim", "position": {"x": 1, "y": 0},
                         "parameters": {"code": "int('abc')"}},
                    ],
                    "edges": [{"id": "e1", "source": "t1", "target": "c1",
                               "sourceHandle": "main", "targetHandle": "main"}],
                },
                "policy": {"retries": 4, "backoff_ms": 20, "retry_on": "transient"},
            }).json()
            run2 = client.post(f"{API}/workflows/{wf2['id']}/run", json={"payload": {}}).json()
            for _ in range(100):
                ex2 = client.get(f"{API}/executions/{run2['execution_id']}").json()
                if ex2["status"] != "running":
                    break
                time.sleep(0.1)
            v2 = next(r for r in ex2["node_runs"] if r["node_name"] == "victim")
            assert v2.get("attempts", 1) == 1, v2
            assert "permanent error, retries skipped" in (v2["error"] or ""), v2
            checks += 1
            print("4. permanent failure skipped the retry wheel (1 attempt)")

            # --- 3. dashboard share audit ------------------------------------------
            ds = client.post(f"{API}/datasets", json={
                "name": f"smoke v51 ds {uuid.uuid4().hex[:6]}",
                "rows": [{"region": "eu", "amount": 5}],
            }).json()
            board = client.post(f"{API}/dashboards", json={
                "name": f"smoke v51 board {uuid.uuid4().hex[:6]}",
                "dataset_ids": [ds["id"]],
            }).json()
            client.post(f"{API}/dashboards/{board['id']}/publish")
            token = client.put(f"{API}/dashboards/{board['id']}/share", json={"enabled": True}).json()["share_token"]
            r_ok = client.get(f"{API}/dashboards/{board['slug']}/runtime?t={token}")
            r_bad = client.get(f"{API}/dashboards/{board['slug']}/runtime?t=nope")
            r_none = client.get(f"{API}/dashboards/{board['slug']}/runtime")
            assert r_ok.status_code == 200 and r_bad.status_code == 403 and r_none.status_code == 403, (
                r_ok.status_code, r_bad.status_code, r_none.status_code)
            audit = client.get(f"{API}/dashboards/{board['id']}/share/audit").json()
            assert audit["total"] == 3, audit
            outcomes = sorted(e["outcome"] for e in audit["events"])
            assert outcomes == ["allowed", "denied", "denied"], audit
            checks += 1
            print(f"5. dashboard share audit: 1 allowed + 2 denied (details: {[e['detail'] for e in audit['events'] if e['outcome'] == 'denied']})")

            # cleanup
            client.delete(f"{API}/dashboards/{board['id']}")
            client.delete(f"{API}/datasets/{ds['id']}")
            client.delete(f"{API}/workflows/{wf['id']}")
            client.delete(f"{API}/workflows/{wf2['id']}")

        print(f"SMOKE OK - {checks} checks passed")
        return 0
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    sys.exit(main())
