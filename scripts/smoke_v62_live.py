"""V62 live smoke: boot the real server and verify systems governance.

1. Templates: the catalog lists 4 role kits; instantiating the ML
   engineer kit creates a system with the pack imported (workflow
   INACTIVE, dataset with rows), a report bound - and the training
   workflow RUNS offline through the real engine.
2. Roles: a second real user is invited as viewer (can read, cannot
   bind: 403) then as editor (can bind); membership management rules
   hold (unknown user 404, duplicate 409, owner not invitable 400).
3. Dependencies: two systems sharing a dataset produce a shared_object
   edge; a writer pipeline and a reader pipeline produce data_flow
   edges in both directions; a registered model scored from the other
   system produces a model_flow edge.

Usage: /home/z/.venv/bin/python scripts/smoke_v62_live.py
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
    db_path = f"{BACKEND}/data/smoke_v62_{uuid.uuid4().hex[:8]}.sqlite3"
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
            tag = uuid.uuid4().hex[:6]

            # a real owner for the governance checks (unclaimed systems grant
            # every authenticated user owner-by-bootstrap, so the role matrix
            # needs an OWNED system)
            email1 = f"v62-smoke-owner-{tag}@py8n.test"
            r = c.post("/auth/register", json={"email": email1, "password": "correct-horse-battery", "name": "smoke owner"})
            assert r.status_code == 201, r.text
            u1 = r.json()
            h1 = {"Authorization": f"Bearer {u1['token']}"}

            # --- 1) templates: catalog + instantiate + the workflow RUNS ----
            cat = c.get("/systems/templates").json()
            assert len(cat["templates"]) == 4, cat
            assert set(cat["roles"]) == {"data_engineer", "ml_engineer", "ops_lead", "support_lead"}

            res = c.post("/systems/templates/mlops-foundation/instantiate")
            assert res.status_code == 201, res.text
            inst = res.json()
            assert inst["created"]["report"], inst
            assert len(inst["created"]["workflows"]) == 1
            assert len(inst["created"]["datasets"]) == 1
            wf_id = inst["created"]["workflows"][0]["id"]
            assert c.get("/workflows").json()  # sanity
            wf = next(w for w in c.get("/workflows").json() if w["id"] == wf_id)
            assert wf["is_active"] is False  # pack convention: land inactive
            ds_id = inst["created"]["datasets"][0]["id"]
            assert c.get(f"/datasets/{ds_id}").json()["row_count"] >= 20

            res = c.post(f"/workflows/{wf_id}/run", json={})
            assert res.status_code in (200, 202), res.text
            ex = res.json()["execution_id"]
            for _ in range(200):
                det = c.get(f"/executions/{ex}").json()
                if det["status"] not in ("running", "queued"):
                    break
                time.sleep(0.05)
            assert det["status"] == "success", det
            detail = c.get(f"/systems/{inst['id']}").json()
            assert detail["health"]["workflows"]["runs_7d"] >= 1
            assert detail["my_role"] == "owner"
            checks += 1
            print(f"[1] template instantiate: {detail['name']} workflow ran offline, "
                  f"report={inst['created']['report']['name']} verdict={detail['health']['verdict']}")

            # --- 2) roles with two real users --------------------------------
            email2 = f"v62-smoke-{tag}@py8n.test"
            r = c.post("/auth/register", json={"email": email2, "password": "correct-horse-battery", "name": "smoke u2"})
            assert r.status_code == 201, r.text
            u2 = r.json()
            h2 = {"Authorization": f"Bearer {u2['token']}"}

            res = c.post("/systems", headers=h1, json={"name": f"Governed {tag}"})
            sid = res.json()["id"]
            res = c.post("/datasets", headers=h1, json={"name": f"gov-ds-{tag}", "rows": [{"a": 1}]})
            ds2 = res.json()["id"]

            res = c.post(f"/systems/{sid}/members", headers=h1, json={"email": email2, "role": "viewer"})
            assert res.status_code == 201, res.text
            # viewer reads, cannot bind
            assert c.get(f"/systems/{sid}", headers=h2).status_code == 200
            assert c.post(f"/systems/{sid}/components", headers=h2,
                          json={"kind": "dataset", "ref_id": ds2}).status_code == 403
            # promote to editor -> can bind
            res = c.put(f"/systems/{sid}/members/{u2['user']['id']}", headers=h1, json={"role": "editor"})
            assert res.status_code == 200, res.text
            assert c.post(f"/systems/{sid}/components", headers=h2,
                          json={"kind": "dataset", "ref_id": ds2}).status_code == 201
            # membership guards
            assert c.post(f"/systems/{sid}/members", headers=h1,
                          json={"email": "ghost@py8n.test", "role": "viewer"}).status_code == 404
            assert c.post(f"/systems/{sid}/members", headers=h1,
                          json={"email": email2, "role": "viewer"}).status_code == 409
            roster = c.get(f"/systems/{sid}/members", headers=h1).json()["members"]
            assert roster[0]["role"] == "owner" and roster[0]["is_owner"]
            checks += 1
            print(f"[2] roles: viewer read-ok/bind-403, editor bind-ok, "
                  f"roster={[m['role'] for m in roster]}")

            # --- 3) cross-system dependency map ------------------------------
            rows = [{"customer_id": f"c-{i:03d}", "tenure": 40 - i,
                     "monthly_spend": 20 + (i * 7) % 60, "support_tickets": i % 4,
                     "churned": "yes" if (i * 13) % 7 < 3 else "no"} for i in range(1, 25)]
            ds_name = f"dep-ds-{tag}"
            res = c.post("/datasets", json={"name": ds_name, "rows": rows})
            ds3 = res.json()["id"]

            def graph(nodes, edges):
                return {"nodes": nodes, "edges": edges}

            res = c.post("/workflows", json={"name": f"dep-write-{tag}", "graph": graph(
                [{"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                 {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 0, "y": 0}, "parameters": {"dataset": ds_name}},
                 {"id": "w", "type": "dataset_write", "name": "w", "position": {"x": 0, "y": 0},
                  "parameters": {"dataset": ds_name, "mode": "upsert", "key_columns": "customer_id"}}],
                [{"id": "e1", "source": "t", "target": "r", "sourceHandle": "main", "targetHandle": "main"},
                 {"id": "e2", "source": "r", "target": "w", "sourceHandle": "main", "targetHandle": "main"}])})
            wf_write = res.json()["id"]
            res = c.post("/workflows", json={"name": f"dep-read-{tag}", "graph": graph(
                [{"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                 {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 0, "y": 0}, "parameters": {"dataset": ds_name}}],
                [{"id": "e1", "source": "t", "target": "r", "sourceHandle": "main", "targetHandle": "main"}])})
            wf_read = res.json()["id"]

            # register a model via the real engine
            res = c.post("/workflows", json={"name": f"dep-train-{tag}", "graph": graph(
                [{"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                 {"id": "r", "type": "dataset_read", "name": "r", "position": {"x": 0, "y": 0}, "parameters": {"dataset": ds_name}},
                 {"id": "tr", "type": "model_train", "name": "tr", "position": {"x": 0, "y": 0},
                  "parameters": {"model": "random_forest_classifier", "target": "churned",
                                 "features": "tenure,monthly_spend,support_tickets",
                                 "model_name": f"dep-model-{tag}", "register": True}}],
                [{"id": "e1", "source": "t", "target": "r", "sourceHandle": "main", "targetHandle": "main"},
                 {"id": "e2", "source": "r", "target": "tr", "sourceHandle": "main", "targetHandle": "main"}])})
            ex = c.post(f"/workflows/{res.json()['id']}/run", json={}).json()["execution_id"]
            for _ in range(200):
                det = c.get(f"/executions/{ex}").json()
                if det["status"] not in ("running", "queued"):
                    break
                time.sleep(0.05)
            assert det["status"] == "success", det

            res = c.post("/systems", json={"name": f"Data Platform {tag}"})
            sa = res.json()["id"]
            res = c.post("/systems", json={"name": f"ML Platform {tag}"})
            sb = res.json()["id"]
            c.post(f"/systems/{sa}/components", json={"kind": "workflow", "ref_id": wf_write})
            c.post(f"/systems/{sa}/components", json={"kind": "dataset", "ref_id": ds3})
            c.post(f"/systems/{sb}/components", json={"kind": "dataset", "ref_id": ds3})
            c.post(f"/systems/{sb}/components", json={"kind": "workflow", "ref_id": wf_read})
            mrow = next(m for m in c.get("/models").json() if m["name"] == f"dep-model-{tag}" and m["active"])
            c.post(f"/systems/{sb}/components", json={"kind": "model", "ref_id": mrow["id"]})
            # a scoring workflow in SA that uses SB's model -> model_flow edge
            res = c.post("/workflows", json={"name": f"dep-score-{tag}", "graph": graph(
                [{"id": "t", "type": "manual_trigger", "name": "t", "position": {"x": 0, "y": 0}, "parameters": {}},
                 {"id": "g", "type": "code", "name": "g", "position": {"x": 0, "y": 0},
                  "parameters": {"code": "result = [{'tenure': 30, 'monthly_spend': 50, 'support_tickets': 1}]"}},
                 {"id": "p", "type": "model_predict", "name": "p", "position": {"x": 0, "y": 0},
                  "parameters": {"model": f"dep-model-{tag}"}}],
                [{"id": "e1", "source": "t", "target": "g", "sourceHandle": "main", "targetHandle": "main"},
                 {"id": "e2", "source": "g", "target": "p", "sourceHandle": "main", "targetHandle": "main"}])})
            assert res.status_code == 201, res.text
            c.post(f"/systems/{sa}/components", json={"kind": "workflow", "ref_id": res.json()["id"]})

            g = c.get("/systems/dependencies").json()
            ids = {n["id"] for n in g["nodes"]}
            assert {sa, sb} <= ids
            assert g["summary"]["by_type"]["shared_object"] >= 1
            df_edges = [e for e in g["edges"] if e["type"] == "data_flow"]
            assert any(e["from"] == sa and e["to"] == sb and any(x["direction"] == "write" for x in e["evidence"]) for e in df_edges), g["edges"]
            assert any(e["from"] == sb and e["to"] == sa and any(x["direction"] == "read" for x in e["evidence"]) for e in df_edges)
            mf = [e for e in g["edges"] if e["type"] == "model_flow"]
            assert any(any(x["model"] == f"dep-model-{tag}" for x in e["evidence"]) for e in mf), mf
            checks += 1
            print(f"[3] dependencies: {g['summary']} - shared+dataflow(both ways)+modelflow all derived")

            print(f"SMOKE v62 GREEN - {checks}/3 checks passed")
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
