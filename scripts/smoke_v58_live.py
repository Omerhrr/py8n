"""V58 live smoke: boot the real server and verify AI Operations.

1. Investigation: a workflow that succeeded once, then broke via a graph
   edit, investigates with the full 7-step checklist, a 'code' cause
   with real evidence, the graph-change hint, and the affected surface.
2. Fail-soft narration: narrate=True against an unreachable bridge keeps
   the findings intact and returns a note instead of an error.
3. Apply proposal: a throttling-style policy patch validates, applies,
   and lands as a new workflow version; bad patches are rejected.

Usage: /home/z/.venv/bin/python scripts/smoke_v58_live.py
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
    db_path = f"{BACKEND}/data/smoke_v58_{uuid.uuid4().hex[:8]}.sqlite3"
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

            res = c.post("/datasets", json={"name": "ai_smoke_ds", "rows": [{"a": 1}]})
            ds = res.json()
            graph = {"nodes": [
                node("t1", "manual_trigger"),
                node("calc", "code", {"code": "result = {'x': 1}"}),
                node("save", "dataset_write", {"dataset": ds["name"], "rows": "{{ nodes.calc.output }}"}),
            ], "edges": [
                {"id": "e1", "source": "t1", "target": "calc", "sourceHandle": "main", "targetHandle": "main"},
                {"id": "e2", "source": "calc", "target": "save", "sourceHandle": "main", "targetHandle": "main"},
            ]}
            res = c.post("/workflows", json={"name": "ai_smoke_wf", "graph": graph})
            assert res.status_code == 201, res.text
            wf = res.json()["id"]

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

            good = run_wf(wf)
            assert good["status"] == "success"
            graph["nodes"][1]["parameters"]["code"] = "result = 1 / 0"
            res = c.put(f"/workflows/{wf}", json={"graph": graph})
            assert res.status_code == 200, res.text
            bad = run_wf(wf)
            assert bad["status"] == "error"

            # --- 1) deterministic investigation ------------------------------
            f = c.post("/ops/ai/investigate", json={"execution_id": bad["id"], "narrate": False}).json()
            steps = [s["step"] for s in f["checklist"]]
            assert steps == [
                "workflow_identified", "failed_execution_identified", "failed_node_identified",
                "error_inspected", "previous_run_compared", "dataset_health_checked",
                "recent_changes_checked",
            ], steps
            assert f["cause"]["kind"] == "code" and "ZeroDivision" in f["cause"]["evidence"]
            by_step = {s["step"]: s for s in f["checklist"]}
            assert by_step["recent_changes_checked"]["detail"].startswith("graph changed")
            assert "ai_smoke_ds" in f["affected"]["datasets"]
            checks += 1
            print(f"[1] investigation: cause={f['cause']['kind']} "
                  f"steps={len(steps)} hints={len(f['hints'])} affected={f['affected']['datasets']}")

            # --- 2) fail-soft narration --------------------------------------
            f2 = c.post("/ops/ai/investigate", json={"execution_id": bad["id"], "narrate": True}).json()
            assert f2["narration"] is None and f2["narration_note"], f2
            checks += 1
            print(f"[2] narration fail-soft: {f2['narration_note']}")

            # --- 3) apply proposal (the 'user executes' half) ------------------
            res = c.post("/ops/ai/apply-proposal", json={
                "workflow_id": wf, "patch": {"retries": 4, "backoff_ms": 8000, "backoff_multiplier": 2.0},
            })
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["policy"]["retries"] == 4 and out["version"]
            res = c.post("/ops/ai/apply-proposal", json={"workflow_id": wf, "patch": {"retries": 99}})
            assert res.status_code == 400
            checks += 1
            print(f"[3] apply proposal: retries={out['policy']['retries']} "
                  f"backoff={out['policy']['backoff_ms']}ms version=v{out['version']}")

            print(f"SMOKE v58 GREEN - {checks}/3 checks passed")
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
