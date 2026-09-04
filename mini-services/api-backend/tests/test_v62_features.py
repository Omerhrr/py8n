"""V62 feature tests: system templates, system-level roles, dependency views.

GOVERNANCE FOR THE SYSTEMS LAYER:

- Role-specific system templates: curated starter kits per role
  (data_engineer / ml_engineer / ops_lead / support_lead). Instantiate
  runs the pack through the SAME import machinery as marketplace
  installs (validation gate + dataset name collision handling), binds
  everything into a real Py8n System and adds the role's report.

- System-level roles: the creator is the single owner (owner_id);
  ``system_members`` holds invited editors (bind/unbind/edit) and
  viewers (read-only). Fail closed: a system you are not part of looks
  nonexistent (404); an action above your role is 403. The owner's role
  is fixed - ownership is never shared.

- Cross-system dependency views: DERIVED at read time, never stored.
  shared_object (same object bound twice), data_flow (a workflow reads/
  writes another system's dataset - node-type aware) and model_flow
  (a workflow scores with another system's model).

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v61).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.main import app
from app.services import executor as executor_mod

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v62-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v62 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"], "email": body["user"]["email"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _run_and_wait(client: httpx.AsyncClient, wf_id: str, headers: dict) -> dict:
    res = await client.post(f"/workflows/{wf_id}/run", headers=headers, json={})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(200):
        res = await client.get(f"/executions/{exec_id}", headers=headers)
        assert res.status_code == 200, res.text
        if res.json()["status"] not in ("running", "queued"):
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def test_v62_system_templates():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"tpl-{tag}", 1)
            h = _auth(user["token"])

            # --- catalog: roles + filter ------------------------------------
            res = await client.get("/systems/templates", headers=h)
            assert res.status_code == 200, res.text
            cat = res.json()
            assert set(cat["roles"]) == {"data_engineer", "ml_engineer", "ops_lead", "support_lead"}
            assert len(cat["templates"]) == 4
            assert all({"slug", "name", "role", "outcomes", "tagline"} <= set(t) for t in cat["templates"])

            res = await client.get("/systems/templates?role=ml_engineer", headers=h)
            slugs = [t["slug"] for t in res.json()["templates"]]
            assert slugs == ["mlops-foundation"]

            res = await client.get("/systems/templates?role=astronaut", headers=h)
            assert res.status_code == 400

            # --- instantiate the ml engineer kit ------------------------------
            res = await client.post("/systems/templates/mlops-foundation/instantiate", headers=h)
            assert res.status_code == 201, res.text
            inst = res.json()
            assert inst["my_role"] == "owner"
            assert len(inst["created"]["workflows"]) == 1
            assert len(inst["created"]["datasets"]) == 1
            assert inst["created"]["report"] is not None
            assert inst["created"]["report"]["cron"] == "0 7 1 * *"
            assert inst["created"]["dashboard"] is None  # ml kit has no dashboard

            # the created workflow landed INACTIVE (pack convention)
            res = await client.get("/workflows", headers=h)
            wf = next(w for w in res.json() if w["id"] == inst["created"]["workflows"][0]["id"])
            assert wf["is_active"] is False

            # the dataset carries sample rows
            res = await client.get(f"/datasets/{inst['created']['datasets'][0]['id']}", headers=h)
            assert res.status_code == 200
            assert res.json()["row_count"] >= 20

            # everything is bound to the system
            res = await client.get(f"/systems/{inst['id']}", headers=h)
            detail = res.json()
            assert len(detail["grouped"]["workflow"]) == 1
            assert len(detail["grouped"]["dataset"]) == 1
            assert len(detail["grouped"]["report"]) == 1
            assert detail["my_role"] == "owner"

            # unknown slug
            res = await client.post("/systems/templates/nope/instantiate", headers=h)
            assert res.status_code == 404

            # data engineer kit also lands a second dataset (pipeline target)
            res = await client.post("/systems/templates/ingestion-quality/instantiate", headers=h)
            assert res.status_code == 201
            assert len(res.json()["created"]["datasets"]) == 2

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass


def test_v62_system_roles():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, f"role-{tag}", 1)
            editor = await _mk_user(client, f"role-{tag}", 2)
            viewer = await _mk_user(client, f"role-{tag}", 3)
            stranger = await _mk_user(client, f"role-{tag}", 4)
            ho = _auth(owner["token"])
            he = _auth(editor["token"])
            hv = _auth(viewer["token"])
            hs = _auth(stranger["token"])

            res = await client.post("/systems", headers=ho, json={"name": f"Governed {tag}"})
            assert res.status_code == 201, res.text
            sys_row = res.json()
            sid = sys_row["id"]
            assert sys_row["my_role"] == "owner"

            # an object to bind
            res = await client.post("/datasets", headers=ho, json={"name": f"role-ds-{tag}", "rows": [{"a": 1}]})
            ds = res.json()
            res = await client.post("/datasets", headers=ho, json={"name": f"role-ds2-{tag}", "rows": [{"a": 2}]})
            ds2 = res.json()

            # --- invites (owner only) --------------------------------------
            res = await client.post(f"/systems/{sid}/members", headers=he,
                                    json={"email": editor["email"], "role": "viewer"})
            assert res.status_code == 404  # fail closed: not a member yet -> invisible

            res = await client.post(f"/systems/{sid}/members", headers=ho,
                                    json={"email": viewer["email"], "role": "viewer"})
            assert res.status_code == 201, res.text
            res = await client.post(f"/systems/{sid}/members", headers=ho,
                                    json={"email": editor["email"], "role": "editor"})
            assert res.status_code == 201

            res = await client.post(f"/systems/{sid}/members", headers=ho,
                                    json={"email": "ghost@py8n.test", "role": "viewer"})
            assert res.status_code == 404  # unknown user
            res = await client.post(f"/systems/{sid}/members", headers=ho,
                                    json={"email": viewer["email"], "role": "viewer"})
            assert res.status_code == 409  # already a member
            res = await client.post(f"/systems/{sid}/members", headers=ho,
                                    json={"email": owner["email"], "role": "editor"})
            assert res.status_code == 400  # the owner cannot be invited
            res = await client.post(f"/systems/{sid}/members", headers=ho,
                                    json={"email": editor["email"], "role": "owner"})
            assert res.status_code == 400  # ownership is not shared

            # --- role matrix --------------------------------------------------
            res = await client.get(f"/systems/{sid}", headers=hv)
            assert res.status_code == 200
            assert res.json()["my_role"] == "viewer"
            res = await client.post(f"/systems/{sid}/components", headers=hv,
                                    json={"kind": "dataset", "ref_id": ds["id"]})
            assert res.status_code == 403  # viewer cannot bind

            res = await client.post(f"/systems/{sid}/components", headers=he,
                                    json={"kind": "dataset", "ref_id": ds["id"]})
            assert res.status_code == 201  # editor binds
            comp_id = res.json()["component_id"]
            res = await client.post(f"/systems/{sid}/components", headers=he,
                                    json={"kind": "dataset", "ref_id": ds2["id"]})
            assert res.status_code == 201
            res = await client.put(f"/systems/{sid}", headers=he, json={"description": "edited by editor"})
            assert res.status_code == 200

            res = await client.delete(f"/systems/{sid}/components/{comp_id}", headers=hv)
            assert res.status_code == 403  # viewer cannot unbind
            res = await client.delete(f"/systems/{sid}", headers=he)
            assert res.status_code == 403  # editor cannot dissolve
            res = await client.delete(f"/systems/{sid}", headers=hv)
            assert res.status_code == 403

            # a stranger sees nothing at all
            res = await client.get(f"/systems/{sid}", headers=hs)
            assert res.status_code == 404
            res = await client.get("/systems", headers=hs)
            assert all(s["id"] != sid for s in res.json())

            # --- member management ---------------------------------------------
            res = await client.get(f"/systems/{sid}/members", headers=hv)
            assert res.status_code == 200  # viewers can see the roster
            roster = res.json()["members"]
            assert roster[0]["role"] == "owner" and roster[0]["is_owner"] is True
            assert {m["role"] for m in roster[1:]} == {"editor", "viewer"}

            res = await client.post(f"/systems/{sid}/members", headers=he,
                                    json={"email": stranger["email"], "role": "viewer"})
            assert res.status_code == 403  # editors cannot invite

            res = await client.put(f"/systems/{sid}/members/{editor['id']}", headers=ho,
                                   json={"role": "viewer"})
            assert res.status_code == 200
            res = await client.post(f"/systems/{sid}/components", headers=he,
                                    json={"kind": "dataset", "ref_id": ds["id"]})
            assert res.status_code == 403  # demoted to viewer

            res = await client.put(f"/systems/{sid}/members/{owner['id']}", headers=ho,
                                   json={"role": "viewer"})
            assert res.status_code == 400  # the owner's role is fixed
            res = await client.delete(f"/systems/{sid}/members/{owner['id']}", headers=ho)
            assert res.status_code == 400  # the owner cannot be removed

            res = await client.put(f"/systems/{sid}/members/{viewer['id']}", headers=ho,
                                   json={"role": "editor"})
            assert res.status_code == 200
            res = await client.delete(f"/systems/{sid}/members/{viewer['id']}", headers=ho)
            assert res.status_code == 204
            res = await client.get(f"/systems/{sid}/members", headers=hv)
            assert res.status_code == 404  # kicked out = invisible again

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass


def test_v62_cross_system_dependencies():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"dep-{tag}", 1)
            stranger = await _mk_user(client, f"dep-{tag}", 2)
            h = _auth(user["token"])

            rows = [{"customer_id": f"c-{i:03d}", "tenure": 40 - i,
                     "monthly_spend": 20 + (i * 7) % 60, "support_tickets": i % 4,
                     "churned": "yes" if (i * 13) % 7 < 3 else "no"}
                    for i in range(1, 25)]
            res = await client.post("/datasets", headers=h,
                                    json={"name": f"dep-customers-{tag}", "rows": rows})
            ds = res.json()
            ds_name = ds["name"]

            # SA: the pipeline that writes the dataset
            wf_write_graph = {"nodes": [_node("t", "manual_trigger"),
                                        _node("r", "dataset_read", {"dataset": ds_name}),
                                        _node("w", "dataset_write", {"dataset": ds_name, "mode": "upsert",
                                                                      "key_columns": "customer_id"})],
                              "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "w")]}
            res = await client.post("/workflows", headers=h,
                                    json={"name": f"write-pipeline-{tag}", "graph": wf_write_graph})
            wf_write = res.json()

            # SB: a reader workflow (graph-only, no run needed)
            wf_read_graph = {"nodes": [_node("t", "manual_trigger"),
                                       _node("r", "dataset_read", {"dataset": ds_name}),
                                       _node("s", "summarize", {"group_by": "churned",
                                                                "aggregations": "tenure:mean"})],
                             "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "s")]}
            res = await client.post("/workflows", headers=h,
                                    json={"name": f"read-pipeline-{tag}", "graph": wf_read_graph})
            wf_read = res.json()

            # a model to share: train + register
            train_graph = {"nodes": [_node("t", "manual_trigger"),
                                     _node("r", "dataset_read", {"dataset": ds_name}),
                                     _node("tr", "model_train",
                                           {"model": "random_forest_classifier", "target": "churned",
                                            "features": "tenure,monthly_spend,support_tickets",
                                            "model_name": f"dep-model-{tag}", "register": True})],
                           "edges": [_edge("e1", "t", "r"), _edge("e2", "r", "tr")]}
            res = await client.post("/workflows", headers=h,
                                    json={"name": f"train-{tag}", "graph": train_graph})
            wf_train = res.json()
            run = await _run_and_wait(client, wf_train["id"], h)
            assert run["status"] == "success", str(run.get("error"))[:400]
            model_name = f"dep-model-{tag}"

            # a scoring workflow in the OTHER system
            score_graph = {"nodes": [_node("t", "manual_trigger"),
                                     _node("g", "code", {"code": "result = [{'tenure': 30, 'monthly_spend': 50, 'support_tickets': 1}]"}),
                                     _node("p", "model_predict", {"model": model_name})],
                           "edges": [_edge("e1", "t", "g"), _edge("e2", "g", "p")]}
            res = await client.post("/workflows", headers=h,
                                    json={"name": f"score-{tag}", "graph": score_graph})
            wf_score = res.json()

            # two systems: SA binds writer+dataset, SB binds dataset+reader+model
            res = await client.post("/systems", headers=h, json={"name": f"Data Platform {tag}"})
            sa = res.json()
            res = await client.post("/systems", headers=h, json={"name": f"ML Platform {tag}"})
            sb = res.json()
            for kind, ref in (("workflow", wf_write["id"]), ("dataset", ds["id"])):
                res = await client.post(f"/systems/{sa['id']}/components", headers=h,
                                        json={"kind": kind, "ref_id": ref})
                assert res.status_code == 201, res.text
            for kind, ref in (("dataset", ds["id"]), ("workflow", wf_read["id"])):
                res = await client.post(f"/systems/{sb['id']}/components", headers=h,
                                        json={"kind": kind, "ref_id": ref})
                assert res.status_code == 201, res.text
            res = await client.get("/models", headers=h)
            mrow = next(m for m in res.json() if m["name"] == model_name and m["active"])
            res = await client.post(f"/systems/{sb['id']}/components", headers=h,
                                    json={"kind": "model", "ref_id": mrow["id"]})
            assert res.status_code == 201
            res = await client.post(f"/systems/{sa['id']}/components", headers=h,
                                    json={"kind": "workflow", "ref_id": wf_score["id"]})
            assert res.status_code == 201

            # --- the graph -----------------------------------------------------
            res = await client.get("/systems/dependencies", headers=h)
            assert res.status_code == 200, res.text
            g = res.json()
            node_ids = {n["id"] for n in g["nodes"]}
            assert {sa["id"], sb["id"]} <= node_ids
            edges = {(e["from"], e["to"], e["type"]): e for e in g["edges"]}

            # shared dataset -> undirected shared_object
            shared = [e for e in g["edges"] if e["type"] == "shared_object"]
            assert shared, "expected a shared_object edge"
            assert any(e["weight"] >= 1 for e in shared)
            ev = shared[0]["evidence"][0]
            assert ev["kind"] == "dataset" and ev["name"] == ds_name

            # writer pipeline in SA -> data_flow SA -> SB
            df_edges = [e for e in g["edges"] if e["type"] == "data_flow"]
            assert df_edges, "expected data_flow edges"
            assert any(e["from"] == sa["id"] and e["to"] == sb["id"] and
                       any(x["direction"] == "write" for x in e["evidence"]) for e in df_edges)
            # reader pipeline in SB -> data_flow SB -> SA
            assert any(e["from"] == sb["id"] and e["to"] == sa["id"] and
                       any(x["direction"] == "read" for x in e["evidence"]) for e in df_edges)

            # scorer in SA uses SB's model -> model_flow SA -> SB
            mf = [e for e in g["edges"] if e["type"] == "model_flow"]
            assert any(e["from"] == sa["id"] and e["to"] == sb["id"] and
                       any(x["model"] == model_name for x in e["evidence"]) for e in mf)

            # filtered subgraph: only edges touching the requested system
            res = await client.get(f"/systems/dependencies?system_id={sb['id']}", headers=h)
            sub = res.json()
            assert all(e["from"] == sb["id"] or e["to"] == sb["id"] for e in sub["edges"])

            # visibility: a stranger's graph has neither system
            res = await client.get("/systems/dependencies", headers=_auth(stranger["token"]))
            assert all(n["id"] not in (sa["id"], sb["id"]) for n in res.json()["nodes"])
            assert res.json()["edges"] == [] or all(
                e["from"] not in (sa["id"], sb["id"]) for e in res.json()["edges"])

    asyncio.run(_go())
    try:
        asyncio.run(_drain_background())
    except RuntimeError:
        pass
