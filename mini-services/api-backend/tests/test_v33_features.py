"""V33 feature tests: the Readymade Automations gallery.

The gallery (GET /templates) ships 16 curated blueprints - the 8 originals
plus 8 v33 automations showcasing the v19-v32 stack (Document AI → dataset,
uptime sentinel, tool-calling agent, respond-to-webhook API, error handler,
memory chatbot, cross-dataset SQL join, lead capture). Every graph must
validate against the engine; installing one creates a real (inactive)
workflow - optionally renamed - and the SQL-join template must actually
RUN offline and produce joined rows in DuckDB.

Same harness as v4-v32: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed names, finally-cleanup + background drain.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.engine.registry import all_definitions
from app.main import app
from app.services import executor as executor_mod
from app.services.templates import TEMPLATES

API = "http://testserver/api/v1"

V33_IDS = {
    "invoice-to-books",
    "uptime-sentinel",
    "research-agent",
    "custom-webhook-reply",
    "error-responder",
    "support-chatbot",
    "csv-join-report",
    "lead-capture-api",
}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _cleanup(workflow_ids: list[str], dataset_ids: list[str]) -> None:
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
        for did in dataset_ids:
            try:
                await client.delete(f"/datasets/{did}")
            except Exception:
                pass
    await _drain_background()


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(120):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def test_v33_gallery_shape_and_validation():
    """Every template: unique id, gallery metadata, valid node types, valid graph."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            # strict version pin lives in the latest wave only (v33 convention)
            hres = await client.get("/health")
            assert hres.status_code == 200 and hres.json()["app"] == "Py8n"  # strict pin moved to v34 (convention)

            res = await client.get("/templates")
            assert res.status_code == 200, res.text
            templates = res.json()
            ids = [t["id"] for t in templates]
            assert len(ids) >= 16 and len(ids) == len(set(ids)), ids
            known_types = {d["type"] for d in all_definitions()}
            for t in templates:
                # gallery metadata (v33 additions are additive + backward compatible)
                assert t["accent"], t["id"]
                assert isinstance(t["tags"], list), t["id"]
                assert t["node_count"] == len(t["node_types"]), t["id"]
                for nt in t["node_types"]:
                    assert nt in known_types, f"{t['id']} ships unknown node type {nt}"
                # detail carries the graph and it validates server-side
                res = await client.get(f"/templates/{t['id']}")
                assert res.status_code == 200, res.text
                assert "graph" in res.json()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids, []))


def test_v33_showcase_templates_present():
    """The 8 new automations exist with badges and the flagship node types."""
    by_id = {t["id"]: t for t in TEMPLATES}
    missing = V33_IDS - set(by_id)
    assert not missing, f"missing v33 templates: {missing}"
    assert by_id["invoice-to-books"]["badge"] == "Doc AI"
    assert "document_extract" in by_id["invoice-to-books"]["graph"]["nodes"][1]["type"]
    assert "dataset_write" in [n["type"] for n in by_id["invoice-to-books"]["graph"]["nodes"]]
    assert "ai_agent" in [n["type"] for n in by_id["support-chatbot"]["graph"]["nodes"]]
    assert "chat_trigger" in [n["type"] for n in by_id["support-chatbot"]["graph"]["nodes"]]
    assert "error_trigger" in [n["type"] for n in by_id["error-responder"]["graph"]["nodes"]]
    types_custom = [n["type"] for n in by_id["custom-webhook-reply"]["graph"]["nodes"]]
    assert types_custom.count("respond_to_webhook") == 2
    assert "sql_query" in [n["type"] for n in by_id["csv-join-report"]["graph"]["nodes"]]
    agent_node = next(n for n in by_id["research-agent"]["graph"]["nodes"] if n["type"] == "ai_agent")
    tool_kinds = [tool["kind"] for tool in agent_node["parameters"]["tools"]]
    assert tool_kinds == ["knowledge", "http"]


def test_v33_install_with_custom_name():
    """POST /use with a body renames the copy; without a body uses the template name."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            res = await client.post("/templates/csv-join-report/use", json={"name": f"Join Test {tag}"})
            assert res.status_code == 201, res.text
            wf = res.json()
            wf_ids.append(wf["id"])
            assert wf["name"] == f"Join Test {tag}"
            assert wf["is_active"] is False
            assert len(wf["graph"]["nodes"]) == 6

            res = await client.post("/templates/lead-capture-api/use")
            assert res.status_code == 201, res.text
            wf2 = res.json()
            wf_ids.append(wf2["id"])
            assert wf2["name"] == "Lead Capture API → Dataset"

            res = await client.post("/templates/does-not-exist/use")
            assert res.status_code == 404, res.text

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids, []))


def test_v33_sql_join_template_runs_offline():
    """The flagship: install the SQL-join automation, run it, verify DuckDB output."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    dataset_ids: list[str] = []

    async def _go():
        async with _client() as client:
            res = await client.post("/templates/csv-join-report/use", json={"name": f"Join Run {tag}"})
            assert res.status_code == 201, res.text
            wf = res.json()
            wf_ids.append(wf["id"])

            execution = await _run_and_wait(client, wf["id"])
            assert execution["status"] == "success", execution.get("error")

            # SQL node output: joined revenue per customer, DESC
            sql_node = next(
                (n for n in execution["node_runs"] if n["node_type"] == "sql_query"), None
            )
            assert sql_node is not None, [n["node_type"] for n in execution["node_runs"]]
            rows = sql_node["output"]["items"]
            assert [r["name"] for r in rows] == ["Acme Corp", "Globex", "Initech"], rows
            assert rows[0]["total"] == 340.0  # 120.0 + 220.0
            assert rows[0]["orders"] == 2

            # both datasets materialized and queryable
            res = await client.get("/datasets")
            assert res.status_code == 200, res.text
            for name in ("demo_orders", "demo_customers"):
                ds = next((d for d in res.json() if d["name"] == name), None)
                assert ds is not None, f"{name} dataset missing"
                dataset_ids.append(ds["id"])
                assert ds["row_count"] >= 3

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids, dataset_ids))
