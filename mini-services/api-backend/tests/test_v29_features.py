"""V29 feature tests: App Builder — Excel → App flagship.

* App CRUD with slug resolution (id OR ci name; unique slugify with -2/-3).
* One-click generation from a dataset: stat cards (count + numeric means),
  breakdown chart on low-cardinality text, full table, create form.
* Config validation: unknown types/aggs/columns, empty forms, dup ids, cap.
* Publish/unpublish guards; runtime served by slug for PUBLISHED apps only.
* Records: create (string values coerced to column dtype), edit by index,
  delete by index; deleting the LAST row keeps the schema alive.
* Blank-first flow: create empty → bind dataset later → generate → publish.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v28).
"""

from __future__ import annotations

import asyncio

import httpx

from app.main import app

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _cleanup(app_refs: list[str], dataset_refs: list[str]) -> None:
    async with _client() as client:
        for ref in app_refs:
            try:
                await client.delete(f"/apps/{ref}")
            except Exception:
                pass
        for ref in dataset_refs:
            try:
                await client.delete(f"/datasets/{ref}")
            except Exception:
                pass


ROWS = [
    {"name": "Alice", "email": "alice@x.io", "plan": "starter", "ltv": 1200, "active": True},
    {"name": "Bob", "email": "bob@x.io", "plan": "pro", "ltv": 3400, "active": True},
    {"name": "Cara", "email": "cara@x.io", "plan": "enterprise", "ltv": 9800, "active": False},
    {"name": "Dan", "email": "dan@x.io", "plan": "starter", "ltv": 900, "active": True},
    {"name": "Eve", "email": "eve@x.io", "plan": "enterprise", "ltv": 12450, "active": True},
    {"name": "Finn", "email": "finn@x.io", "plan": "pro", "ltv": 3100, "active": False},
    {"name": "Gus", "email": "gus@x.io", "plan": "enterprise", "ltv": 10200, "active": True},
    {"name": "Hana", "email": "hana@x.io", "plan": "starter", "ltv": 1500, "active": True},
]


async def _make_dataset(client: httpx.AsyncClient, name: str) -> dict:
    r = await client.post("/datasets", json={"name": name, "rows": ROWS})
    assert r.status_code == 201, r.text
    return r.json()


def test_v29_apps_crud_and_slugs():
    async def _go():
        async with _client() as c:
            r = await c.get("/health")
            assert r.json()["version"] == "1.29.0"

            # blank-first: no dataset, empty components, draft
            r = await c.post("/apps", json={"name": "v29 Team"})
            assert r.status_code == 201, r.text
            a1 = r.json()
            assert a1["slug"] == "v29-team" and a1["status"] == "draft"
            assert a1["config"].get("components") == [] and a1["dataset_id"] is None

            # different name, same slug → -2 suffix (dots are legal in names)
            r = await c.post("/apps", json={"name": "v29. Team"})
            assert r.status_code == 201, r.text
            a2 = r.json()
            assert a2["slug"] == "v29-team-2"

            # case-insensitive duplicate name → 409
            r = await c.post("/apps", json={"name": "V29 TEAM"})
            assert r.status_code == 409

            # resolve by id AND by case-insensitive name
            r = await c.get(f"/apps/{a1['id']}")
            assert r.status_code == 200 and r.json()["name"] == "v29 Team"
            r = await c.get("/apps/v29. TEAM")
            assert r.status_code == 200 and r.json()["id"] == a2["id"]

            # rename re-slugs
            r = await c.patch(f"/apps/{a1['id']}", json={"name": "v29 Crew", "description": "renamed"})
            assert r.status_code == 200
            assert r.json()["slug"] == "v29-crew" and r.json()["description"] == "renamed"

            # delete → gone
            r = await c.delete(f"/apps/{a1['id']}")
            assert r.status_code == 204
            r = await c.get("/apps/v29 Crew")
            assert r.status_code == 404
            await c.delete(f"/apps/{a2['id']}")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v29 Crew", "v29. Team", "v29 Team"], []))


async def _published_app(c: httpx.AsyncClient, ds: dict, name: str) -> dict:
    r = await c.post("/apps", json={"name": name, "dataset_id": ds["id"]})
    assert r.status_code == 201, r.text
    app_row = r.json()
    comps = app_row["config"]["components"]
    r = await c.post(f"/apps/{app_row['id']}/publish")
    assert r.status_code == 200, r.text
    return r.json() | {"_components": comps}


def test_v29_generate_and_runtime():
    async def _go():
        async with _client() as c:
            ds = await _make_dataset(c, "v29 App Clients")
            app_row = await _published_app(c, ds, "v29 Clients CRM")
            comps = app_row["_components"]
            types = [x["type"] for x in comps]
            assert types == ["stat", "stat", "chart", "table", "form"]
            assert comps[0] == {"id": "stat_total", "type": "stat", "label": "Total records", "agg": "count"}
            assert comps[1]["agg"] == "avg" and comps[1]["column"] == "ltv"
            assert comps[1]["label"] == "Avg Ltv"
            assert comps[2]["group_by"] == "plan" and comps[2]["agg"] == "count"
            assert comps[3]["columns"] == ["name", "email", "plan", "ltv", "active"]
            assert comps[4]["fields"] == ["name", "email", "plan", "ltv", "active"]

            # runtime by slug — stats + chart verified by hand
            r = await c.get(f"/apps/{app_row['slug']}/runtime")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["app"]["name"] == "v29 Clients CRM"
            assert body["dataset"]["row_count"] == 8
            assert body["dataset"]["schema_json"][3] == {"name": "ltv", "dtype": "integer"}
            assert body["stats"]["stat_total"] == 8
            assert abs(body["stats"]["stat_ltv"] - 5318.75) < 0.01  # 42550 / 8
            chart = body["chart"]
            assert chart["chart_type"] == "bar"
            assert dict(zip(chart["labels"], chart["values"])) == {"starter": 3, "pro": 2, "enterprise": 3}

            # draft / unknown apps have NO runtime
            r = await c.post("/apps", json={"name": "v29 Draft Only", "dataset_id": ds["id"]})
            draft = r.json()
            r = await c.get(f"/apps/{draft['slug']}/runtime")
            assert r.status_code == 404
            r = await c.get("/apps/no-such-app/runtime")
            assert r.status_code == 404
            await c.delete(draft["id"])

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v29 Clients CRM", "v29 Draft Only"], ["v29 App Clients"]))


def test_v29_records_crud_and_coercion():
    async def _go():
        async with _client() as c:
            ds = await _make_dataset(c, "v29 Rec Clients")
            app_row = await _published_app(c, ds, "v29 Rec CRM")
            slug = app_row["slug"]

            # create with STRING values → coerced into column dtypes
            r = await c.post(f"/apps/{slug}/records", json={
                "record": {"name": "Zoe", "email": "zoe@x.io", "plan": "pro", "ltv": "5500", "active": "true"}
            })
            assert r.status_code == 201, r.text
            assert r.json()["row_count"] == 9

            r = await c.get(f"/apps/{slug}/records", params={"offset": 8, "limit": 5})
            page = r.json()
            assert page["row_count"] == 9
            zoe = page["rows"][0]
            assert zoe["ltv"] == 5500 and zoe["active"] is True  # coerced

            # runtime stats react to the new record
            r = await c.get(f"/apps/{slug}/runtime")
            assert r.json()["stats"]["stat_total"] == 9
            assert abs(r.json()["stats"]["stat_ltv"] - 48050 / 9) < 0.01

            # unknown fields rejected (schema drift guard)
            r = await c.post(f"/apps/{slug}/records", json={"record": {"nope": 1}})
            assert r.status_code == 400 and "unknown fields" in r.json()["detail"]

            # edit by index
            r = await c.patch(f"/apps/{slug}/records/8", json={"record": {"ltv": 6000}})
            assert r.status_code == 200, r.text
            assert r.json()["record"]["ltv"] == 6000
            r = await c.get(f"/apps/{slug}/runtime")
            assert abs(r.json()["stats"]["stat_ltv"] - 48550 / 9) < 0.01

            # out-of-range edit/delete → 404
            r = await c.patch(f"/apps/{slug}/records/99", json={"record": {"ltv": 1}})
            assert r.status_code == 404
            r = await c.delete(f"/apps/{slug}/records/99")
            assert r.status_code == 404

            # delete by index
            r = await c.delete(f"/apps/{slug}/records/8")
            assert r.status_code == 200 and r.json()["row_count"] == 8
            r = await c.get(f"/apps/{slug}/runtime")
            assert r.json()["stats"]["stat_total"] == 8

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v29 Rec CRM"], ["v29 Rec Clients"]))


def test_v29_delete_last_row_keeps_schema():
    async def _go():
        async with _client() as c:
            ds = await _make_dataset(c, "v29 Solo Clients")
            app_row = await _published_app(c, ds, "v29 Solo CRM")
            slug = app_row["slug"]

            # delete rows one by one until NONE are left
            for expected in (7, 6, 5, 4, 3, 2, 1, 0):
                r = await c.delete(f"/apps/{slug}/records/0")
                assert r.status_code == 200 and r.json()["row_count"] == expected

            # schema survives: columns still listed, records endpoint empty
            r = await c.get(f"/apps/{slug}/records")
            body = r.json()
            assert body["rows"] == [] and body["row_count"] == 0
            assert body["columns"] == ["name", "email", "plan", "ltv", "active"]

            # runtime stays healthy; stats degrade to zero/None
            r = await c.get(f"/apps/{slug}/runtime")
            body = r.json()
            assert body["stats"]["stat_total"] == 0
            assert body["stats"]["stat_ltv"] is None
            assert body["chart"]["labels"] == []

            # and the app still accepts new records (columns known again)
            r = await c.post(f"/apps/{slug}/records", json={"record": {"name": "New", "plan": "pro"}})
            assert r.status_code == 201 and r.json()["row_count"] == 1

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v29 Solo CRM"], ["v29 Solo Clients"]))


def test_v29_config_validation():
    async def _go():
        async with _client() as c:
            ds = await _make_dataset(c, "v29 Val Clients")
            r = await c.post("/apps", json={"name": "v29 Validator", "dataset_id": ds["id"]})
            app_row = r.json()
            aid = app_row["id"]

            bad_configs = [
                ({"components": [{"id": "x", "type": "carousel"}]}, "unknown type"),
                ({"components": [{"id": "x", "type": "stat", "agg": "median"}]}, "agg"),
                ({"components": [{"id": "x", "type": "stat", "agg": "avg"}]}, "requires a column"),
                ({"components": [{"id": "x", "type": "stat", "agg": "avg", "column": "nope"}]}, "not in dataset"),
                ({"components": [{"id": "x", "type": "table", "columns": ["nope"]}]}, "not in dataset"),
                ({"components": [{"id": "x", "type": "table", "columns": [], "page_size": 500}]}, "page_size"),
                ({"components": [{"id": "x", "type": "form", "fields": []}]}, "at least one field"),
                ({"components": [{"id": "x", "type": "form", "fields": ["ghost"]}]}, "not in dataset"),
                ({"components": [{"id": "x", "type": "chart", "chart_type": "dome"}]}, "chart_type"),
                ({"components": [{"id": "x", "type": "chart", "group_by": "ghost"}]}, "not in dataset"),
                ({"components": [{"id": "x", "type": "chart", "group_by": "plan"}, {"id": "x", "type": "chart", "group_by": "plan"}]}, "duplicate"),
                ({"components": "nope"}, "must be a list"),
            ]
            for config, hint in bad_configs:
                r = await c.patch(f"/apps/{aid}", json={"config": config})
                assert r.status_code == 400, f"{config} → {r.status_code}"
                assert hint in r.json()["detail"], f"{config} → {r.json()['detail']}"

            # >24 components
            r = await c.patch(f"/apps/{aid}", json={"config": {"components": [
                {"id": f"s{i}", "type": "stat", "agg": "count"} for i in range(25)
            ]}})
            assert r.status_code == 400 and "too many" in r.json()["detail"]

            # a valid custom config lands
            good = {"components": [
                {"id": "t1", "type": "stat", "label": "Pipeline", "agg": "sum", "column": "ltv"},
                {"id": "p1", "type": "chart", "title": "Plan mix", "chart_type": "pie", "group_by": "plan", "agg": "count"},
                {"id": "tb", "type": "table", "title": "Clients", "columns": ["name", "plan"], "page_size": 5},
                {"id": "fm", "type": "form", "title": "New client", "fields": ["name", "plan"], "submit_label": "Save"},
            ]}
            r = await c.patch(f"/apps/{aid}", json={"config": good})
            assert r.status_code == 200 and len(r.json()["config"]["components"]) == 4

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v29 Validator"], ["v29 Val Clients"]))


def test_v29_blank_first_and_publish_guards():
    async def _go():
        async with _client() as c:
            # blank-first with an explicit (broken) config — allowed pre-bind,
            # validation only runs against a BOUND dataset's schema
            broken = {"components": [{"id": "b", "type": "stat", "agg": "avg"}]}
            r = await c.post("/apps", json={"name": "v29 Blank First", "config": broken})
            row = r.json()
            assert row["config"] == broken

            # generate / publish before any dataset → 409
            r = await c.post(f"/apps/{row['id']}/generate")
            assert r.status_code == 409 and "Bind a dataset" in r.json()["detail"]
            r = await c.post(f"/apps/{row['id']}/publish")
            assert r.status_code == 409 and "Bind a dataset" in r.json()["detail"]

            # bind via tri-state PATCH → the BROKEN stored config now blocks publish
            ds = await _make_dataset(c, "v29 Blank Clients")
            r = await c.patch(f"/apps/{row['id']}", json={"dataset_id": ds["id"]})
            assert r.status_code == 200 and r.json()["dataset_name"] == "v29 Blank Clients"
            r = await c.post(f"/apps/{row['id']}/publish")
            assert r.status_code == 400 and "Invalid config" in r.json()["detail"]

            # generate rebuilds a valid layout → publish → config edits LOCK while published
            r = await c.post(f"/apps/{row['id']}/generate")
            assert r.status_code == 200
            types = [x["type"] for x in r.json()["config"]["components"]]
            assert types == ["stat", "stat", "chart", "table", "form"]
            r = await c.post(f"/apps/{row['id']}/publish")
            assert r.status_code == 200 and r.json()["status"] == "published"
            slug = r.json()["slug"]
            r = await c.get(f"/apps/{slug}/runtime")
            assert r.status_code == 200
            r = await c.patch(f"/apps/{row['id']}", json={"config": {"components": []}})
            assert r.status_code == 409 and "Unpublish" in r.json()["detail"]

            # unbind with "" is refused?? — no: unbind is a PATCH, allowed while published,
            # but publishing gates live at publish-time; runtime still works off the binding
            r = await c.post(f"/apps/{row['id']}/unpublish")
            assert r.status_code == 200 and r.json()["status"] == "draft"
            r = await c.get(f"/apps/{slug}/runtime")
            assert r.status_code == 404

            # unbind → dataset_name clears
            r = await c.patch(f"/apps/{row['id']}", json={"dataset_id": ""})
            assert r.status_code == 200 and r.json()["dataset_id"] is None

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(["v29 Blank First"], ["v29 Blank Clients"]))
