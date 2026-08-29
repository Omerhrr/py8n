"""V6 feature tests: workflow templates + per-node input capture."""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.engine import GraphRunner
from app.engine.nodes.base import BaseNode  # noqa: F401  (registry side-effects)
from app.engine.runner import validate_graph_document
from app.main import app
from app.engine.schema import GraphSpec  # noqa: E402  (used above)
from app.services.templates import TEMPLATES, template_summary  # noqa: E402

API = "http://testserver/api/v1"


# -------------------------------------------------- all templates validate
def test_every_template_graph_is_valid_and_offline_ones_run():
    assert len(TEMPLATES) >= 8
    for t in TEMPLATES:
        spec = validate_graph_document(t["graph"])  # raises on broken graph
        assert spec.nodes, t["id"]
        summary = template_summary(t)
        assert {"id", "name", "description", "category", "icon", "docs", "node_count"} <= set(summary.keys()), t["id"]


# ------------------------------------------------------- input capture
def test_node_runs_capture_their_input():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"n": 5}}},
            {"id": "double", "type": "code",
             "parameters": {"code": "result = {'v': input_data['payload']['n'] * 2}\n"}},
            {"id": "label", "type": "set_variable",
             "parameters": {"assignments": {"shown": "value={{ nodes.double.output.result.v }}"}, "keep_input": False}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "double"},
            {"id": "e2", "source": "double", "target": "label"},
        ],
    }
    result = asyncio.run(
        GraphRunner(GraphSpec.model_validate(graph), workflow_id="wf", workflow_name="T").run()
    )
    assert result["status"] == "success", result["error"]
    runs = {r["node_id"]: r for r in result["node_runs"]}
    # trigger has no incoming input
    assert "input" not in runs["t"]
    # downstream nodes record what they received (the trigger's full envelope)
    assert runs["double"]["input"]["payload"] == {"n": 5}
    assert runs["label"]["input"] == {"result": {"v": 10}}


def test_merge_node_captures_keyed_multi_input():
    graph = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {"x": 1}}},
            {"id": "a", "type": "code", "parameters": {"code": "result = {'side': 'a'}\n"}},
            {"id": "b", "type": "code", "parameters": {"code": "result = {'side': 'b'}\n"}},
            {"id": "m", "type": "merge", "parameters": {"mode": "combine"}},
        ],
        "edges": [
            {"id": "e1", "source": "t", "target": "a"},
            {"id": "e2", "source": "t", "target": "b"},
            {"id": "e3", "source": "a", "target": "m"},
            {"id": "e4", "source": "b", "target": "m"},
        ],
    }
    result = asyncio.run(
        GraphRunner(GraphSpec.model_validate(graph), workflow_id="wf", workflow_name="T").run()
    )
    assert result["status"] == "success", result["error"]
    merge_run = next(r for r in result["node_runs"] if r["node_id"] == "m")
    assert set(merge_run["input"].keys()) == {"a", "b"}
    assert merge_run["input"]["a"] == {"result": {"side": "a"}}


# --------------------------------------------------- templates API + use
def test_templates_api_list_detail_and_use():
    tag = uuid.uuid4().hex[:6]

    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=API
        ) as client:
            res = await client.get("/templates")
            assert res.status_code == 200
            listing = res.json()
            assert len(listing) == len(TEMPLATES)
            assert {"id", "name", "category", "node_count"} <= set(listing[0].keys())
            assert all("graph" not in t for t in listing), "listing must stay lean"

            detail = await client.get("/templates/approval-gate")
            assert detail.status_code == 200
            assert "graph" in detail.json()

            missing = await client.get("/templates/nope")
            assert missing.status_code == 404

            # use -> real inactive workflow, runnable immediately
            created_ids = []
            tpl_id = "data-pipeline"
            use = await client.post(f"/templates/{tpl_id}/use")
            assert use.status_code == 201, use.text
            wf = use.json()
            created_ids.append(wf["id"])
            assert wf["name"] == "Split → Filter → Aggregate"
            assert wf["is_active"] is False

            acc = await client.post(f"/workflows/{wf['id']}/run", json={})
            exec_id = acc.json()["execution_id"]
            for _ in range(40):
                res = await client.get(f"/executions/{exec_id}")
                if res.json()["status"] != "running":
                    break
                await asyncio.sleep(0.1)
            assert res.json()["status"] == "success", res.json()
            total = next(
                n for n in res.json()["node_runs"] if n["node_id"] == "total"
            )
            assert total["output"]["value"] == 340  # 120 + 220

            # renamed instance stays independent
            rename = await client.put(
                f"/workflows/{wf['id']}", json={"name": f"pipeline-{tag}", "is_active": False}
            )
            assert rename.status_code == 200

            bad = await client.post("/templates/nope/use")
            assert bad.status_code == 404

            for wid in created_ids:
                await client.delete(f"/workflows/{wid}")

    asyncio.run(_go())


from app.engine.schema import GraphSpec  # noqa: E402  (used above)
