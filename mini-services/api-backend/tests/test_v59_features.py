"""V59 feature tests: AI System Builder.

The roadmap's Describe -> Discover -> Clarify -> Design -> Build loop:

SYNTHESIS: POST /systems turns a plain-language description into a
SystemSpec - persona detected (data_engineer vs business = adaptive
technical depth), source/schedule parsed, a component checklist
(core always on; recommended/optional selected only when the description
calls for them), and clarifying questions for the unknowns. DETERMINISTIC
so it can never propose a component py8n cannot build; optional LLM
enhancement via the sandbox bridge is fail-soft.

INTERVIEW: POST /systems/{id}/answers folds answers back into the spec
(table, contract fields, dedupe keys, webhook). COMPONENTS: tick/untick
with dependency validation (quality gate needs the contract; the
backbone cannot be removed).

BUILD: POST /systems/{id}/build translates the SELECTED components into
REAL primitives - dataset, workflow graph from registered node types
(schedule/db source/dedupe/upsert write/AI summary), execution policy,
schema contract (error mode under the quality gate), auto-generated
dashboard, scheduled report, failure-notification rule - and lands the
refs for one-glance review.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v58).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.main import app
from app.services import executor as executor_mod
from app.services.system_builder import parse_schedule

API = "http://testserver/api/v1"

ENGINEER_DESC = (
    "I need a pipeline that pulls orders from Postgres every hour, validates the schema, "
    "handles late-arriving records, deduplicates them, writes to a curated dataset, "
    "and alerts me if quality drops"
)
BUSINESS_DESC = "send me a daily report of yesterday's sales"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v59-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v59 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    return {"token": body["token"], "id": body["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _selected(spec: dict) -> set[str]:
    return {c["id"] for c in spec["components"] if c["selected"]}


def test_v59_schedule_parser():
    assert parse_schedule("runs every hour") == {"mode": "interval", "interval_seconds": 3600}
    assert parse_schedule("every 6 hours") == {"mode": "interval", "interval_seconds": 21600}
    assert parse_schedule("every 15 minutes") == {"mode": "interval", "interval_seconds": 900}
    assert parse_schedule("daily at 9")["cron"] == "0 9 * * *"
    assert parse_schedule("daily at 9:30")["cron"] == "30 9 * * *"
    assert parse_schedule("every morning")["cron"] == "0 8 * * *"
    assert parse_schedule("whenever you feel like it") is None


def test_v59_synthesis_and_interview():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"syn-{tag}", 1)
            h = _auth(user["token"])

            # --- describe: the data-engineer ask from the roadmap ------------
            res = await client.post("/systems", headers=h, json={"description": ENGINEER_DESC})
            assert res.status_code == 201, res.text
            d = res.json()
            spec = d["spec"]
            assert d["persona"] == "data_engineer" and spec["persona"] == "data_engineer"
            assert spec["source"]["kind"] == "db" and spec["source"]["backend"] == "postgres"
            assert spec["schedule"]["interval_seconds"] == 3600
            sel = _selected(spec)
            assert {"target_dataset", "pipeline_workflow", "schedule"} <= sel  # core
            assert {"schema_contract", "dedupe", "incremental", "quality_gate", "failure_notification"} <= sel
            assert "scheduled_report" not in sel  # no report language
            assert spec["lookback_hours"] == 24.0  # late-arriving records
            qkeys = {q["key"] for q in spec["questions"]}
            assert {"table", "fields", "dedupe_keys", "webhook_url"} <= qkeys

            # --- clarify: answer the interview --------------------------------
            res = await client.post(f"/systems/{d['id']}/answers", headers=h, json={"answers": {
                "table": "orders",
                "fields": "id:integer, region:text, revenue:number, updated_at:datetime",
                "dedupe_keys": "id",
                "webhook_url": "https://hooks.example.com/py8n",
            }})
            assert res.status_code == 200, res.text
            spec = res.json()["spec"]
            assert spec["source"]["table"] == "orders"
            assert [f["name"] for f in spec["fields"]] == ["id", "region", "revenue", "updated_at"]
            assert spec["fields"][0]["dtype"] == "integer" and spec["fields"][2]["dtype"] == "number"
            assert spec["dedupe_keys"] == ["id"]
            assert spec["webhook_url"] == "https://hooks.example.com/py8n"
            assert all(q["answered"] for q in spec["questions"])

            # --- design: component toggles with dependency guards -------------
            # untick the contract first, then the quality gate must refuse to stand alone
            res = await client.post(f"/systems/{d['id']}/components", headers=h,
                                    json={"component_id": "schema_contract", "selected": False})
            assert res.status_code == 200
            res = await client.post(f"/systems/{d['id']}/components", headers=h,
                                    json={"component_id": "quality_gate", "selected": True})
            assert res.status_code == 400
            res = await client.post(f"/systems/{d['id']}/components", headers=h,
                                    json={"component_id": "target_dataset", "selected": False})
            assert res.status_code == 400  # the backbone stays
            res = await client.post(f"/systems/{d['id']}/components", headers=h,
                                    json={"component_id": "nope", "selected": True})
            assert res.status_code == 400
            # put the contract back
            res = await client.post(f"/systems/{d['id']}/components", headers=h,
                                    json={"component_id": "schema_contract", "selected": True})
            assert res.status_code == 200

            # --- business language gets business depth -------------------------
            res = await client.post("/systems", headers=h, json={"description": BUSINESS_DESC})
            assert res.status_code == 201, res.text
            d2 = res.json()
            assert d2["persona"] == "business"
            sel2 = _selected(d2["spec"])
            assert "scheduled_report" in sel2  # "report" language
            assert "incremental" not in sel2 and "dedupe" not in sel2  # no engineer plumbing
            assert "quality_gate" not in sel2

            # --- llm enhancement is fail-soft -----------------------------------
            res = await client.post("/systems", headers=h, json={
                "description": ENGINEER_DESC, "use_llm": True,
            })
            assert res.status_code == 201, res.text
            d3 = res.json()
            assert d3["spec"]["source"]["kind"] == "db"  # deterministic spec intact
            assert any("LLM enhancement skipped" in n for n in d3["spec"]["notes"])

            # --- scoping + validation --------------------------------------------
            other = await _mk_user(client, f"syn-{tag}", 2)
            res = await client.get(f"/systems/{d['id']}", headers=_auth(other["token"]))
            assert res.status_code == 404
            res = await client.post("/systems", headers=h, json={"description": "short"})
            assert res.status_code in (400, 422)

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


def test_v59_build():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, f"build-{tag}", 1)
            h = _auth(user["token"])

            res = await client.post("/systems", headers=h, json={"description": ENGINEER_DESC})
            assert res.status_code == 201, res.text
            d = res.json()
            res = await client.post(f"/systems/{d['id']}/answers", headers=h, json={"answers": {
                "table": "orders",
                "fields": "id:integer, region:text, revenue:number, updated_at:datetime",
                "dedupe_keys": "id",
                "webhook_url": "https://hooks.example.com/py8n",
            }})
            assert res.status_code == 200, res.text

            # add the optional pieces the engineer description didn't name
            for cid in ("retry_policy", "dashboard", "scheduled_report", "ai_summary"):
                res = await client.post(f"/systems/{d['id']}/components", headers=h,
                                        json={"component_id": cid, "selected": True})
                assert res.status_code == 200, res.text

            # --- BUILD --------------------------------------------------------
            res = await client.post(f"/systems/{d['id']}/build", headers=h)
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["status"] == "built"
            built = out["built"]
            assert built["dataset_id"] and built["dataset_name"]
            assert built["workflow_id"] and built["workflow_name"]
            assert built["dashboard_id"] and built["report_id"]
            assert built["notification_rule_id"]
            assert built["contract_version"] == 1 and built["on_violation"] == "error"
            assert built["policy"] and built["policy"]["retries"] == 2

            # the workflow graph is real: schedule -> db_source -> dedupe -> write -> llm
            res = await client.get(f"/workflows/{built['workflow_id']}", headers=h)
            assert res.status_code == 200, res.text
            wf = res.json()
            types = [n["type"] for n in wf["graph"]["nodes"]]
            assert types == ["schedule_trigger", "db_source", "remove_duplicates", "dataset_write", "llm_chat"], types
            write = next(n for n in wf["graph"]["nodes"] if n["type"] == "dataset_write")
            assert write["parameters"]["dataset"] == built["dataset_name"]
            assert write["parameters"]["mode"] == "upsert"
            assert write["parameters"]["watermark_column"] == "updated_at"
            assert write["parameters"]["key_columns"] == ["id"]
            assert write["parameters"]["lookback"] == 24.0 * 3600
            src = next(n for n in wf["graph"]["nodes"] if n["type"] == "db_source")
            assert src["parameters"]["backend"] == "postgres"
            assert src["parameters"]["table"] == "orders"
            assert wf["policy"]["retries"] == 2
            assert wf["is_active"] is False  # activate after filling credentials

            # the contract is real and enforced in error mode
            res = await client.get(f"/datasets/{built['dataset_id']}/contract", headers=h)
            assert res.status_code == 200, res.text
            contract = res.json()
            cols = {c["name"]: c["dtype"] for c in contract["columns"]}
            assert cols == {"id": "integer", "region": "text", "revenue": "number", "updated_at": "datetime"}
            assert contract["on_violation"] == "error"

            # dashboard + report + notification rule are real rows
            res = await client.get("/dashboards", headers=h)
            assert any(b["id"] == built["dashboard_id"] for b in res.json())
            res = await client.get("/reports", headers=h)
            assert any(r["id"] == built["report_id"] for r in res.json())
            res = await client.get("/notifications", headers=h)
            assert any(r["id"] == built["notification_rule_id"] for r in res.json())

            # a built system is frozen: no rebuild, no more interview edits
            res = await client.post(f"/systems/{d['id']}/build", headers=h)
            assert res.status_code == 400
            res = await client.post(f"/systems/{d['id']}/answers", headers=h,
                                    json={"answers": {"table": "other"}})
            assert res.status_code == 400

            # the drafts list shows it
            res = await client.get("/systems", headers=h)
            row = next(x for x in res.json() if x["id"] == d["id"])
            assert row["status"] == "built" and row["built_refs"]["workflow_id"]

            # scoping
            other = await _mk_user(client, f"build-{tag}", 2)
            res = await client.get(f"/systems/{d['id']}", headers=_auth(other["token"]))
            assert res.status_code == 404

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
