"""V31 feature tests: Dashboards as First-Class Objects.

* Boards are the analytical mirror of apps: MULTI-dataset (every component
  carries its own dataset_id), multiple charts per board, read-only, plus a
  text/narrative component. Published boards serve GET /dashboards/{slug}/runtime
  for the public /d/{slug} page.
* Config validation: component types (stat|chart|table|text), per-type params,
  live dataset resolution on every write (404 on unknown ids), ≥1 component,
  published configs are locked (PATCH → 409).
* compute_config() renders EVERY component; the draft preview endpoint and the
  published runtime share one payload builder; missing datasets degrade to
  empty content (a board never 500s because a component outlived its dataset).
* line charts sort labels ascending; pie slices cap at 8.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v30).
"""

from __future__ import annotations

import asyncio

import httpx

from app.main import app

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _cleanup(dash_refs: list[str], dataset_refs: list[str]) -> None:
    async with _client() as client:
        for ref in dash_refs:
            try:
                await client.delete(f"/dashboards/{ref}")
            except Exception:
                pass
        for ref in dataset_refs:
            try:
                await client.delete(f"/datasets/{ref}")
            except Exception:
                pass


CRM_ROWS = [
    {"name": "Alice", "plan": "starter", "ltv": 1200},
    {"name": "Bob", "plan": "pro", "ltv": 3400},
    {"name": "Cara", "plan": "enterprise", "ltv": 9800},
    {"name": "Dan", "plan": "starter", "ltv": 900},
    {"name": "Eve", "plan": "enterprise", "ltv": 12450},
    {"name": "Finn", "plan": "pro", "ltv": 3100},
]
BILL_ROWS = [
    {"invoice": "INV-1", "status": "paid", "amount": 250},
    {"invoice": "INV-2", "status": "open", "amount": 480},
    {"invoice": "INV-3", "status": "paid", "amount": 310},
    {"invoice": "INV-4", "status": "void", "amount": 90},
]


async def _seed_two_datasets(client: httpx.AsyncClient) -> tuple[str, str]:
    r = await client.post("/datasets", json={"name": "v31 CRM", "rows": CRM_ROWS})
    assert r.status_code == 201, r.text
    crm_id = r.json()["id"]
    r = await client.post("/datasets", json={"name": "v31 Billing", "rows": BILL_ROWS})
    assert r.status_code == 201, r.text
    bill_id = r.json()["id"]
    return crm_id, bill_id


# ---------------------------------------------------------------------------
def test_v31_dashboard_crud_and_validation():
    """Create (blank + generate), name rules, config validation matrix, locks."""

    async def _go():
        async with _client() as client:
            # health gate — strict pin lives in the latest wave's tests only
            r = await client.get("/health")
            assert r.status_code == 200
            assert r.json()["version"] == "1.31.0"
            # node registry untouched by v31 (dashboards are objects, not nodes)
            r = await client.get("/node-definitions")
            assert len(r.json()["definitions"]) == 36

            crm_id, bill_id = await _seed_two_datasets(client)

            # ---- blank create + slug/describe
            r = await client.post(
                "/dashboards", json={"name": "v31 Board", "description": "ops wall"}
            )
            assert r.status_code == 201, r.text
            board = r.json()
            assert board["slug"] == "v31-board"
            assert board["status"] == "draft"
            assert board["config"]["components"] == []

            # ---- duplicate name (case-insensitive) 409; invalid name 400
            r = await client.post("/dashboards", json={"name": "v31 BOARD"})
            assert r.status_code == 409
            r = await client.post("/dashboards", json={"name": "!bad name!"})
            assert r.status_code == 400

            # ---- blank create is tolerated even with generate defaulting true
            r = await client.post(
                "/dashboards", json={"name": "v31 Genless", "generate": True}
            )
            assert r.status_code == 201 and r.json()["config"]["components"] == []

            # ---- generated create from two datasets
            r = await client.post(
                "/dashboards",
                json={"name": "v31 Gen Board", "dataset_ids": [crm_id, bill_id]},
            )
            assert r.status_code == 201, r.text
            gen = r.json()
            comps = gen["config"]["components"]
            types = [c["type"] for c in comps]
            # 2 count stats + 2 avg stats + 2 charts + 1 table
            assert types.count("stat") == 4
            assert types.count("chart") == 2
            assert types.count("table") == 1
            # per-component dataset binding is preserved
            for c in comps:
                assert c["dataset_id"] in (crm_id, bill_id)
            chart = comps[types.index("chart")]
            assert chart["group_by"] == "plan"  # lowest-cardinality text col of CRM
            assert chart["chart_type"] == "bar"
            table = comps[types.index("table")]
            assert table["dataset_id"] == crm_id  # first dataset owns the table

            # ---- validation matrix on PATCH config
            def _patch_cfg(cfg):
                return client.patch(f"/dashboards/{board['id']}", json={"config": cfg})

            base = [{"id": "k", "type": "stat", "dataset_id": crm_id, "label": "N", "agg": "count"}]

            r = await _patch_cfg({"components": []})
            assert r.status_code == 400  # at least one component
            r = await _patch_cfg({"components": [{"id": "x", "type": "gauge"}]})
            assert r.status_code == 400 and "unknown type" in r.json()["detail"]
            r = await _patch_cfg({"components": [{"id": "s", "type": "stat", "dataset_id": crm_id, "label": "S", "agg": "median"}]})
            assert r.status_code == 400 and "agg" in r.json()["detail"]
            r = await _patch_cfg({"components": [{"id": "s", "type": "stat", "dataset_id": crm_id, "label": "S", "agg": "sum"}]})
            assert r.status_code == 400 and "requires a column" in r.json()["detail"]
            r = await _patch_cfg({"components": [{"id": "s", "type": "stat", "dataset_id": "nope", "label": "S", "agg": "count"}]})
            assert r.status_code == 404  # live dataset resolution
            r = await _patch_cfg({"components": [{"id": "c", "type": "chart", "dataset_id": crm_id, "title": "T", "chart_type": "scatter", "group_by": "plan"}]})
            assert r.status_code == 400 and "chart_type" in r.json()["detail"]
            r = await _patch_cfg({"components": [{"id": "c", "type": "chart", "dataset_id": crm_id, "title": "T", "chart_type": "bar", "group_by": "nope"}]})
            assert r.status_code == 400 and "group_by" in r.json()["detail"]
            r = await _patch_cfg({"components": [{"id": "c", "type": "chart", "dataset_id": crm_id, "chart_type": "bar", "group_by": "plan"}]})
            assert r.status_code == 400 and "title" in r.json()["detail"]
            r = await _patch_cfg({"components": [{"id": "t", "type": "table", "dataset_id": crm_id, "title": "T", "columns": ["ghost"]}]})
            assert r.status_code == 400 and "not in dataset schema" in r.json()["detail"]
            r = await _patch_cfg({"components": [{"id": "t", "type": "table", "dataset_id": crm_id, "title": "T", "columns": ["name"], "limit": 500}]})
            assert r.status_code == 400 and "limit" in r.json()["detail"]
            r = await _patch_cfg({"components": [{"id": "x", "type": "text"}]})
            assert r.status_code == 400 and "title or a body" in r.json()["detail"]
            dup = base + [{"id": "k", "type": "text", "title": "dup"}]
            r = await _patch_cfg({"components": dup})
            assert r.status_code == 400 and "duplicate component id" in r.json()["detail"]

            # ---- a valid multi-component config lands
            valid = {
                "components": [
                    {"id": "k1", "type": "stat", "dataset_id": crm_id, "label": "Clients", "agg": "count"},
                    {"id": "k2", "type": "stat", "dataset_id": bill_id, "label": "Paid sum", "agg": "sum", "column": "amount"},
                    {"id": "c1", "type": "chart", "dataset_id": crm_id, "title": "By plan", "chart_type": "bar", "group_by": "plan", "agg": "count"},
                    {"id": "c2", "type": "chart", "dataset_id": bill_id, "title": "Amount by status", "chart_type": "line", "group_by": "status", "agg": "avg", "column": "amount"},
                    {"id": "t1", "type": "table", "dataset_id": crm_id, "title": "Latest", "columns": ["name", "plan", "ltv"], "limit": 4},
                    {"id": "n1", "type": "text", "title": "Read me", "body": "Refreshes each minute"},
                ]
            }
            r = await _patch_cfg(valid)
            assert r.status_code == 200, r.text
            assert len(r.json()["config"]["components"]) == 6

            # ---- publish, then config PATCH is locked
            r = await client.post(f"/dashboards/{board['id']}/publish")
            assert r.status_code == 200, r.text
            r = await _patch_cfg(valid)
            assert r.status_code == 409 and "Unpublish" in r.json()["detail"]
            # rename still works on a published board (governance-free op)
            r = await client.patch(f"/dashboards/{board['id']}", json={"description": "renamed desc"})
            assert r.status_code == 200

            # ---- unpublish → config editable again
            r = await client.post(f"/dashboards/{board['id']}/unpublish")
            assert r.status_code == 200 and r.json()["status"] == "draft"

            # ---- list + get by name + delete
            r = await client.get("/dashboards")
            assert any(b["name"] == "v31 Board" for b in r.json())
            r = await client.get("/dashboards/v31 Board")
            assert r.status_code == 200
            r = await client.post(f"/dashboards/{board['id']}/generate")
            assert r.status_code == 200  # now has dataset refs → regeneration works

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v31 Board", "v31 Gen Board", "v31 Genless"], ["v31 CRM", "v31 Billing"]))


def test_v31_preview_and_runtime_numbers():
    """Preview (draft) + runtime (published) render EVERY component with exact numbers."""

    async def _go():
        async with _client() as client:
            crm_id, bill_id = await _seed_two_datasets(client)
            cfg = {
                "components": [
                    {"id": "k1", "type": "stat", "dataset_id": crm_id, "label": "Clients", "agg": "count"},
                    {"id": "k2", "type": "stat", "dataset_id": bill_id, "label": "Paid sum", "agg": "sum", "column": "amount"},
                    {"id": "c1", "type": "chart", "dataset_id": crm_id, "title": "By plan", "chart_type": "bar", "group_by": "plan", "agg": "count"},
                    {"id": "c2", "type": "chart", "dataset_id": bill_id, "title": "Avg amount by status", "chart_type": "line", "group_by": "status", "agg": "avg", "column": "amount"},
                    {"id": "c3", "type": "chart", "dataset_id": bill_id, "title": "Count by status", "chart_type": "pie", "group_by": "status", "agg": "count"},
                    {"id": "t1", "type": "table", "dataset_id": crm_id, "title": "Latest", "columns": ["name", "ltv"], "limit": 2},
                    {"id": "n1", "type": "text", "title": "Read me", "body": "Board note"},
                ]
            }
            r = await client.post(
                "/dashboards", json={"name": "v31 Numbers", "config": cfg}
            )
            assert r.status_code == 201, r.text
            board = r.json()

            # ---- preview on DRAFT works and carries exact numbers
            r = await client.post(f"/dashboards/{board['id']}/preview")
            assert r.status_code == 200, r.text
            payload = r.json()
            assert payload["dashboard"]["status"] == "draft"
            comps = {c["id"]: c for c in payload["components"]}
            assert comps["k1"]["value"] == 6
            # billing sum over ALL rows: 250 + 480 + 310 + 90 = 1130 (stats have no filter)
            assert comps["k2"]["value"] == 1130
            assert comps["c1"]["labels"] == ["starter", "pro", "enterprise"]
            assert comps["c1"]["values"] == [2, 2, 2]
            # line chart: labels sorted ascending; avg amounts open=480, paid=280, void=90
            assert comps["c2"]["chart_type"] == "line"
            assert comps["c2"]["labels"] == ["open", "paid", "void"]
            assert comps["c2"]["values"] == [480, 280, 90]
            # pie renders too (3 slices < cap 8)
            assert comps["c3"]["chart_type"] == "pie"
            assert dict(zip(comps["c3"]["labels"], comps["c3"]["values"])) == {"paid": 2, "open": 1, "void": 1}
            # table: top 2 rows, only requested columns, row_count is the FULL count
            assert comps["t1"]["columns"] == ["name", "ltv"]
            assert [row["name"] for row in comps["t1"]["rows"]] == ["Alice", "Bob"]
            assert comps["t1"]["row_count"] == 6
            assert comps["n1"]["title"] == "Read me"

            # ---- runtime is 404 while draft
            r = await client.get(f"/dashboards/{board['slug']}/runtime")
            assert r.status_code == 404

            # ---- publish → runtime serves the same numbers
            r = await client.post(f"/dashboards/{board['id']}/publish")
            assert r.status_code == 200
            r = await client.get(f"/dashboards/{board['slug']}/runtime")
            assert r.status_code == 200, r.text
            rt = r.json()
            assert rt["dashboard"]["slug"] == board["slug"]
            assert {d["name"] for d in rt["datasets"]} == {"v31 CRM", "v31 Billing"}
            comps_rt = {c["id"]: c for c in rt["components"]}
            assert comps_rt["k1"]["value"] == 6
            assert comps_rt["k2"]["value"] == 1130
            assert comps_rt["c1"]["values"] == [2, 2, 2]

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v31 Numbers"], ["v31 CRM", "v31 Billing"]))


def test_v31_slugs_and_regenerate():
    """Rename can collide slugs (suffix -2); regenerate re-lays-out from referenced datasets."""

    async def _go():
        async with _client() as client:
            crm_id, bill_id = await _seed_two_datasets(client)

            # distinct names → distinct slugs
            r1 = await client.post("/dashboards", json={"name": "v31 Slug One"})
            r2 = await client.post("/dashboards", json={"name": "v31 Slug Two"})
            assert r1.status_code == 201 and r2.status_code == 201
            a, b = r1.json(), r2.json()
            assert a["slug"] == "v31-slug-one" and b["slug"] == "v31-slug-two"

            # renaming B to a name that slugifies into A's slug suffixes -2
            # (names differ only by a trailing dash, so the name check passes)
            r = await client.patch(f"/dashboards/{b['id']}", json={"name": "v31 Slug One-"})
            assert r.status_code == 200, r.text
            assert r.json()["slug"] == "v31-slug-one-2"

            # regenerate with NO dataset refs → 409
            r = await client.post(f"/dashboards/{a['id']}/generate")
            assert r.status_code == 409

            # seed a config referencing both, regenerate re-lays-out
            cfg = {
                "components": [
                    {"id": "seed", "type": "stat", "dataset_id": crm_id, "label": "N", "agg": "count"},
                    {"id": "seed2", "type": "stat", "dataset_id": bill_id, "label": "M", "agg": "count"},
                ]
            }
            r = await client.patch(f"/dashboards/{a['id']}", json={"config": cfg})
            assert r.status_code == 200
            r = await client.post(f"/dashboards/{a['id']}/generate")
            assert r.status_code == 200, r.text
            comps = r.json()["config"]["components"]
            types = [c["type"] for c in comps]
            assert types.count("stat") == 4 and types.count("chart") == 2 and types.count("table") == 1

            # published generate is refused
            await client.post(f"/dashboards/{a['id']}/publish")
            r = await client.post(f"/dashboards/{a['id']}/generate")
            assert r.status_code == 409

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v31 Slug One", "v31 Slug One-"], ["v31 CRM", "v31 Billing"]))


def test_v31_board_survives_dataset_deletion():
    """A published board stays renderable (200, empty content) when a component's
    dataset is deleted later — compute is tolerant, never 500."""

    async def _go():
        async with _client() as client:
            crm_id, _ = await _seed_two_datasets(client)
            cfg = {
                "components": [
                    {"id": "k1", "type": "stat", "dataset_id": crm_id, "label": "Clients", "agg": "count"},
                    {"id": "c1", "type": "chart", "dataset_id": crm_id, "title": "By plan", "chart_type": "bar", "group_by": "plan", "agg": "count"},
                ]
            }
            r = await client.post("/dashboards", json={"name": "v31 Survivor", "config": cfg})
            board = r.json()
            r = await client.post(f"/dashboards/{board['id']}/publish")
            assert r.status_code == 200

            r = await client.get(f"/dashboards/{board['slug']}/runtime")
            assert r.status_code == 200
            comps = {c["id"]: c for c in r.json()["components"]}
            assert comps["k1"]["value"] == 6

            # delete the dataset underneath the live board
            r = await client.delete(f"/datasets/{crm_id}")
            assert r.status_code in (200, 204)

            r = await client.get(f"/dashboards/{board['slug']}/runtime")
            assert r.status_code == 200, r.text  # never 500
            rt = r.json()
            comps = {c["id"]: c for c in rt["components"]}
            assert comps["k1"]["value"] is None  # count over a missing frame → None
            assert comps["c1"]["labels"] == [] and comps["c1"]["values"] == []
            assert rt["datasets"] == []

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v31 Survivor"], ["v31 CRM", "v31 Billing"]))
