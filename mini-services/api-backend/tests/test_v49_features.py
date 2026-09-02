"""V49 feature tests: share-surface auditing + dashboard report images.

Grant audit log: every request through a PROTECTED app runtime surface
leaves one audit event (grant snapshot + action + allowed/denied) -
denials at the gate, scoped views/lists/creates, out-of-scope edit
attempts, read-only rejections; grants carry access aggregates; the trail
endpoint returns newest-first and the per-app cap keeps the newest events.

Dashboard report images: ``fmt=png`` renders every computed component
(stats, bar/line/area, pie/donut, scatter, tables) into one shareable
image through the SAME compute path as the JSON snapshot - one broken
component degrades to a placeholder instead of failing the run.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v48).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.main import app

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod

    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _mk_dataset(client: httpx.AsyncClient, name: str, rows: list[dict]) -> str:
    res = await client.post("/datasets", json={"name": name, "rows": rows})
    assert res.status_code == 201, res.text
    return res.json()["id"]


# ---------------------------------------------------------------------------
# 1) definitions: strict pins live HERE (newest wave)
# ---------------------------------------------------------------------------
def test_v49_definitions():
    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.json()["version"] == "1.49.0", res.json()
            res = await client.get("/node-definitions")
            defs = res.json()["definitions"]
            assert len(defs) >= 47, f"v49 adds no nodes; expected at least 47 types, got {len(defs)}"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ---------------------------------------------------------------------------
# 2) grant audit log
# ---------------------------------------------------------------------------
def test_v49_grant_audit():
    tag = uuid.uuid4().hex[:8]
    ds_refs, app_refs = [], []

    async def _go():
        async with _client() as client:
            rows = (
                [{"region": "eu", "name": f"eu{i}", "mrr": 10 + i} for i in range(1, 7)]
                + [{"region": "us", "name": f"us{i}", "mrr": 50 + i} for i in range(1, 7)]
            )
            ds_id = await _mk_dataset(client, f"v49-audit-{tag}", rows)
            ds_refs.append(f"v49-audit-{tag}")
            config = {"components": [
                {"id": "k1", "type": "stat", "label": "Rows", "agg": "count"},
                {"id": "t1", "type": "table", "title": "Records", "columns": ["region", "name", "mrr"], "page_size": 20},
            ]}
            res = await client.post("/apps", json={"name": f"v49-audit-{tag}", "dataset_id": ds_id, "config": config})
            assert res.status_code == 201, res.text
            app_row = res.json()
            app_refs.append(app_row["id"])
            await client.post(f"/apps/{app_row['id']}/publish")
            slug = app_row["slug"]

            res = await client.post(f"/apps/{app_row['id']}/grants", json={
                "name": "EU team", "column": "region", "op": "eq", "value": "eu",
            })
            assert res.status_code == 201, res.text
            eu_grant = res.json()
            assert eu_grant["access_count"] == 0 and eu_grant["last_access_at"] is None
            tok = eu_grant["token"]

            # --- denials at the gate are logged before the 403 ---
            res = await client.get(f"/apps/{slug}/runtime")
            assert res.status_code == 403
            res = await client.get(f"/apps/{slug}/runtime?t=wrong-token")
            assert res.status_code == 403

            # --- grant viewer traffic is logged with the grant snapshot ---
            res = await client.get(f"/apps/{slug}/runtime?t={tok}")
            assert res.status_code == 200 and res.json()["scope"]["grant"] == "EU team"
            res = await client.get(f"/apps/{slug}/records?t={tok}")
            assert res.status_code == 200
            res = await client.post(f"/apps/{slug}/records?t={tok}", json={
                "record": {"region": "eu", "name": "audited", "mrr": 3},
            })
            assert res.status_code == 201, res.text

            # read-only rejection (in-grant cannot create)
            res = await client.post(f"/apps/{app_row['id']}/grants", json={
                "name": "US read", "column": "region", "op": "in", "value": ["us"],
            })
            us_grant = res.json()
            res = await client.post(f"/apps/{slug}/records?t={us_grant['token']}", json={
                "record": {"region": "us", "name": "blocked", "mrr": 1},
            })
            assert res.status_code == 403

            # out-of-scope PATCH attempt (raw index of a us row)
            res = await client.get(f"/datasets/{ds_id}/rows?limit=100")
            all_rows = res.json()["rows"]
            us_index = next(i for i, r in enumerate(all_rows) if r["name"] == "us1")
            res = await client.patch(f"/apps/{slug}/records/{us_index}?t={tok}", json={"record": {"mrr": 1}})
            assert res.status_code == 404

            # --- the audit trail saw everything ---
            res = await client.get(f"/apps/{app_row['id']}/grants/audit?limit=100")
            assert res.status_code == 200, res.text
            events = res.json()
            assert len(events) >= 7, events
            actions = [e["action"] for e in events]
            for expected in ("view_runtime", "list_records", "create_record", "update_record"):
                assert expected in actions, (expected, actions)
            # newest first (create_record is the last allowed action before
            # the read-only + out-of-scope denials)
            assert events[0]["action"] in ("update_record", "list_records")
            denials = [e for e in events if e["outcome"] == "denied"]
            assert len(denials) >= 3, denials
            assert any(e["detail"] == "missing token" for e in denials)
            assert any(e["detail"] == "invalid token" for e in denials)
            assert any("out of scope" in (e["detail"] or "") for e in denials)
            assert any("read-only" in (e["detail"] or "") for e in denials)
            # gate denials have no grant; scoped traffic carries the snapshot
            gated = [e for e in denials if e["grant_name"] is None]
            assert all(e["grant_id"] is None for e in gated)
            allowed = [e for e in events if e["outcome"] == "allowed" and e["grant_name"]]
            assert all(e["grant_name"] == "EU team" for e in allowed if e["action"] != "create_record" or e["grant_id"])

            # --- grants list carries access aggregates ---
            res = await client.get(f"/apps/{app_row['id']}/grants")
            grants = {g["name"]: g for g in res.json()}
            assert grants["EU team"]["access_count"] >= 3, grants["EU team"]
            assert grants["EU team"]["last_access_at"] is not None
            assert grants["US read"]["access_count"] == 0

            # --- limit param bounds the response ---
            res = await client.get(f"/apps/{app_row['id']}/grants/audit?limit=2")
            assert len(res.json()) == 2

            # --- legacy open apps are never logged ---
            res = await client.post("/apps", json={"name": f"v49-open-{tag}", "dataset_id": ds_id, "config": config})
            open_app = res.json()
            app_refs.append(open_app["id"])
            await client.post(f"/apps/{open_app['id']}/publish")
            await client.get(f"/apps/{open_app['slug']}/runtime")
            await client.get(f"/apps/{open_app['slug']}/records")
            res = await client.get(f"/apps/{open_app['id']}/grants/audit")
            assert res.json() == [], res.json()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        _cleanup(ds_refs, app_refs)


def _cleanup(ds_refs: list[str], app_refs: list[str]) -> None:
    async def _go():
        async with _client() as client:
            for ref in app_refs:
                await client.delete(f"/apps/{ref}")
            for name in ds_refs:
                res = await client.get("/datasets")
                match = next((d for d in res.json() if d["name"] == name), None)
                if match:
                    await client.delete(f"/datasets/{match['id']}")

    asyncio.run(_go())


def test_v49_grant_audit_cap(monkeypatch):
    """The per-app cap keeps the NEWEST events (trim runs on insert)."""
    from app.api import apps as apps_mod

    tag = uuid.uuid4().hex[:8]
    ds_refs, app_refs = [], []
    monkeypatch.setattr(apps_mod, "GRANT_AUDIT_CAP", 3)

    async def _go():
        async with _client() as client:
            rows = [{"region": "eu", "name": f"e{i}", "mrr": i} for i in range(5)]
            ds_id = await _mk_dataset(client, f"v49-cap-{tag}", rows)
            ds_refs.append(f"v49-cap-{tag}")
            res = await client.post("/apps", json={
                "name": f"v49-cap-{tag}", "dataset_id": ds_id,
                "config": {"components": []},
            })
            app_row = res.json()
            app_refs.append(app_row["id"])
            await client.post(f"/apps/{app_row['id']}/publish")
            res = await client.put(f"/apps/{app_row['id']}/share", json={"enabled": True})
            full_tok = res.json()["share_token"]

            for _ in range(5):
                await client.get(f"/apps/{app_row['slug']}/runtime?t={full_tok}")
            res = await client.get(f"/apps/{app_row['id']}/grants/audit?limit=200")
            events = res.json()
            assert len(events) == 3, f"cap should keep exactly 3, got {len(events)}"
            assert all(e["action"] == "view_runtime" and e["outcome"] == "allowed" for e in events)

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        _cleanup(ds_refs, app_refs)


# ---------------------------------------------------------------------------
# 3) dashboard report images (png)
# ---------------------------------------------------------------------------
def test_v49_dashboard_png_reports():
    tag = uuid.uuid4().hex[:8]
    ds_refs, dashboard_ids, report_ids, artifact_ids = [], [], [], []

    async def _go():
        async with _client() as client:
            rows = []
            for i in range(1, 11):
                rows.append({"region": "eu" if i % 2 else "us", "name": f"r{i}", "mrr": 10.0 * i})
            ds_id = await _mk_dataset(client, f"v49-board-{tag}", rows)
            ds_refs.append(f"v49-board-{tag}")

            dash = await client.post("/dashboards", json={
                "name": f"v49-board-{tag}",
                "config": {"components": [
                    {"id": "s1", "type": "stat", "dataset_id": ds_id, "label": "Rows", "agg": "count"},
                    {"id": "c1", "type": "chart", "dataset_id": ds_id, "title": "MRR by region",
                     "chart_type": "bar", "group_by": "region", "agg": "sum", "column": "mrr"},
                    {"id": "c2", "type": "chart", "dataset_id": ds_id, "title": "Share",
                     "chart_type": "donut", "group_by": "region", "agg": "count"},
                    {"id": "t1", "type": "table", "dataset_id": ds_id, "title": "Records",
                     "columns": ["region", "name", "mrr"], "page_size": 5},
                ]},
            })
            assert dash.status_code == 201, dash.text
            dashboard_ids.append(dash.json()["id"])

            # png is valid for dashboards, NOT for datasets
            res = await client.post("/reports", json={
                "name": f"board-png-{tag}", "source_type": "dashboard",
                "source_id": dashboard_ids[0], "fmt": "png", "cron": "0 7 * * *",
            })
            assert res.status_code == 201, res.text
            png_report = res.json()
            report_ids.append(png_report["id"])
            res = await client.post("/reports", json={
                "name": f"ds-png-{tag}", "source_type": "dataset",
                "source_id": ds_id, "fmt": "png", "cron": "0 7 * * *",
            })
            assert res.status_code == 400, "png must not be valid for dataset sources"

            # run now -> a real PNG artifact
            res = await client.post(f"/reports/{png_report['id']}/run")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["run"]["ok"] is True, body
            artifact_ids.append(body["run"]["artifact_id"])
            res = await client.get(f"/artifacts/{body['run']['artifact_id']}/content")
            assert res.status_code == 200, res.text
            assert res.headers["content-type"].startswith("image/png"), res.headers["content-type"]
            assert res.content[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG magic header"
            assert len(res.content) > 5_000, f"suspiciously small image: {len(res.content)} bytes"

            # json snapshot still works (regression)
            res = await client.post("/reports", json={
                "name": f"board-json-{tag}", "source_type": "dashboard",
                "source_id": dashboard_ids[0], "fmt": "json", "cron": "30 7 * * *",
            })
            json_report = res.json()
            report_ids.append(json_report["id"])
            res = await client.post(f"/reports/{json_report['id']}/run")
            assert res.status_code == 200 and res.json()["run"]["ok"], res.text
            artifact_ids.append(res.json()["run"]["artifact_id"])
            res = await client.get(f"/artifacts/{res.json()['run']['artifact_id']}/content")
            assert res.headers["content-type"].startswith("application/json")

            # report rows remember fmt + artifact
            res = await client.get("/reports")
            row = next(r for r in res.json() if r["id"] == png_report["id"])
            assert row["fmt"] == "png" and row["last_status"] == "ok"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
        _cleanup_reports(ds_refs, dashboard_ids, report_ids, artifact_ids)


def _cleanup_reports(ds_refs, dashboard_ids, report_ids, artifact_ids) -> None:
    async def _go():
        async with _client() as client:
            for rid in report_ids:
                await client.delete(f"/reports/{rid}")
            for aid in artifact_ids:
                await client.delete(f"/artifacts/{aid}")
            for did in dashboard_ids:
                await client.delete(f"/dashboards/{did}")
            res = await client.get("/datasets")
            for name in ds_refs:
                match = next((d for d in res.json() if d["name"] == name), None)
                if match:
                    await client.delete(f"/datasets/{match['id']}")

    asyncio.run(_go())


def test_v49_png_renderer_units():
    """Unit level: mixed + malformed components degrade, never raise."""
    from app.services.report_images import render_dashboard_png

    components = [
        {"id": "s1", "type": "stat", "label": "Rows", "value": 12},
        {"id": "c1", "type": "chart", "title": "bar", "chart_type": "bar", "labels": ["a", "b"], "values": [1, 2]},
        {"id": "c2", "type": "chart", "title": "line", "chart_type": "line", "labels": ["x", "y", "z"], "values": [3, 1, 2]},
        {"id": "c3", "type": "chart", "title": "area", "chart_type": "area", "labels": ["x", "y"], "values": [1, 5]},
        {"id": "c4", "type": "chart", "title": "pie", "chart_type": "pie", "labels": ["p", "q"], "values": [2, 2]},
        {"id": "c5", "type": "chart", "title": "scatter", "chart_type": "scatter", "x": "x", "y": "y",
         "points": [{"x": 1, "y": 2}, {"x": 2, "y": 3}]},
        {"id": "c6", "type": "chart", "title": "empty", "chart_type": "bar", "labels": [], "values": []},
        {"id": "c7", "type": "table", "title": "table", "columns": ["a", "b"], "rows": [{"a": 1, "b": 2}]},
        {"id": "c8", "type": "markdown", "title": "md"},
        {"id": "c9", "type": "chart", "title": "broken", "chart_type": "bar", "labels": ["a"], "values": ["not-a-number"]},
        None,  # junk entry is dropped, not fatal
    ]
    data = render_dashboard_png("Unit Board", components)  # type: ignore[arg-type]
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(data) > 10_000

    empty = render_dashboard_png("Empty Board", [])
    assert empty[:8] == b"\x89PNG\r\n\x1a\n"
