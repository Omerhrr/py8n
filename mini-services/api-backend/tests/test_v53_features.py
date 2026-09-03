"""V53 feature tests: data observability surface, incremental-pipeline
deepening (upsert + watermark, lookback, per-run stats).

OBSERVABILITY: GET /observability/events stitches the tables that already
own the truth - dataset_versions (writes), execution logs (run outcomes),
report delivery events (push-outs), app/dashboard audit rows (denied share
access) - into one derived, owner-scoped, severity-typed stream, and
GET /observability/overview composes fleet-wide health (budget-capped
dataset profiling), pipeline reliability, ingestion checkpoints and
delivery outcomes plus the newest error-severity incidents. Nothing is
stored, so the stream cannot drift from what actually happened.

INCREMENTAL DEEPENING: dataset_write gains the incremental-UPSERT combo
(watermark_column + key_columns: rows beyond the checkpoint MERGE on key,
so late-arriving updates never duplicate), a lookback window (rewinds the
comparison baseline - numeric units or ISO seconds - so boundary rows are
re-admitted; the stored checkpoint itself only ever moves forward), and
per-run stats recorded on the IngestionState (rows in/written/skipped/
updated/inserted) surfaced through the ingestion-states API and the new
dataset-page checkpoints section. An empty payload is now a clean no-op
(scheduled sources legitimately return nothing); a NON-empty payload
missing the cursor column still fails loudly.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v52).
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


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph, "is_active": False})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(200):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def _find_node_run(execution: dict, node_name: str) -> dict | None:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name:
            return run
    return None


async def _mk_dataset(client: httpx.AsyncClient, name: str, rows: list[dict]) -> str:
    res = await client.post("/datasets", json={"name": name, "rows": rows})
    assert res.status_code == 201, res.text
    return res.json()["id"]


# ===========================================================================
# 1) rewind_watermark: numeric, ISO, text, none
# ===========================================================================
def test_v53_rewind_watermark():
    from app.services.ingestion import rewind_watermark

    assert rewind_watermark("100", 5) == "95.0"
    assert rewind_watermark("100", 0) == "100"
    assert rewind_watermark("100", None) == "100"
    # ISO datetimes rewind by SECONDS
    assert rewind_watermark("2026-01-01T00:00:00+00:00", 3600) == "2025-12-31T23:00:00+00:00"
    assert rewind_watermark("2026-01-01T00:00:00Z", 60).startswith("2025-12-31T23:59:00")
    # arbitrary text cannot rewind - stays put (stable, resumable)
    assert rewind_watermark("abc", 5) == "abc"
    assert rewind_watermark(None, 5) is None


# ===========================================================================
# 2) filter_incremental: lookback re-admits boundary rows, checkpoint never
#    rewinds; empty payload is a no-op; missing column still fails loudly
# ===========================================================================
def test_v53_filter_incremental_lookback():
    from app.db import AsyncSessionLocal
    from app.services import datasets as ds_svc
    from app.services import ingestion as ing_svc

    tag = uuid.uuid4().hex[:8]
    ds_name = f"v53-lookback-svc-{tag}"

    async def _go():
        async with AsyncSessionLocal() as session:
            ds = await ds_svc.create_from_df(session, ds_name, __import__("pandas").DataFrame(), source="test")
            await session.flush()

            # numeric: first run has NO checkpoint -> lookback is a no-op, everything lands
            rows = [{"ts": "7", "v": "late"}, {"ts": "8", "v": "boundary"}, {"ts": "9.9", "v": "in-window"}, {"ts": "5", "v": "still-old"}, {"ts": "12", "v": "ahead"}]
            fresh, st, before = await ing_svc.filter_incremental(session, ds, rows, "ts", "k1", lookback=2.5)
            assert [r["v"] for r in fresh] == ["late", "boundary", "in-window", "still-old", "ahead"]
            assert before is None  # first run: nothing stored yet
            assert st.watermark == "12"  # the mark already moved to the best SEEN value
            await ing_svc.advance(session, st, st.watermark, len(fresh))
            assert st.watermark == "12"

            # next run WITH lookback 2.5: baseline = 12 - 2.5 = 9.5 -> ts 10 re-admitted
            rows2 = [{"ts": "10", "v": "re-admitted"}, {"ts": "6", "v": "older-than-baseline"}]
            fresh2, st2, before2 = await ing_svc.filter_incremental(session, ds, rows2, "ts", "k1", lookback=2.5)
            assert [r["v"] for r in fresh2] == ["re-admitted"]
            assert before2 == "12"
            await ing_svc.advance(session, st2, None, 0)
            assert st2.watermark == "12"  # the checkpoint itself NEVER moves backwards

            # zero lookback = strict v50 behaviour (baseline IS the checkpoint)
            fresh3, _, _ = await ing_svc.filter_incremental(session, ds, [{"ts": "12", "v": "same"}, {"ts": "13", "v": "new"}], "ts", "k1")
            assert [r["v"] for r in fresh3] == ["new"]

            # ISO: checkpoint rewinds by seconds
            iso_rows = [{"ts": "2026-05-01T12:00:00+00:00", "v": "a"}]
            _, st3, _ = await ing_svc.filter_incremental(session, ds, iso_rows, "ts", "iso")
            await ing_svc.advance(session, st3, "2026-05-01T12:00:00+00:00", 1)
            iso_rows2 = [{"ts": "2026-05-01T11:59:30+00:00", "v": "late-30s"}]
            fresh4, _, before4 = await ing_svc.filter_incremental(session, ds, iso_rows2, "ts", "iso", lookback=60)
            assert [r["v"] for r in fresh4] == ["late-30s"]
            assert before4 == "2026-05-01T12:00:00+00:00"

            # EMPTY payload: clean no-op (scheduled sources return nothing)
            fresh5, st5, before5 = await ing_svc.filter_incremental(session, ds, [], "ts", "k1")
            assert fresh5 == [] and before5 == "13"  # 13 was the best value seen so far

            # NON-empty payload missing the cursor column: still a loud failure
            try:
                await ing_svc.filter_incremental(session, ds, [{"nope": 1}], "ts", "k1")
                raise AssertionError("expected ValueError for missing watermark column")
            except ValueError as exc:
                assert "watermark column" in str(exc)

            await session.commit()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ===========================================================================
# 3) engine: incremental mode with lookback (append semantics preserved,
#    checkpoint advances to the BEST seen value, never the rewound one)
# ===========================================================================
def test_v53_incremental_lookback_engine():
    tag = uuid.uuid4().hex[:8]
    ds_name = f"v53-incr-lookback-{tag}"

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {
                        "dataset": ds_name,
                        "mode": "incremental",
                        "watermark_column": "ts",
                        "ingestion_key": "pipe1",
                        "lookback": 2.5,
                    }),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            wf = await _make_workflow(client, f"v53-incr-lookback-{tag}", graph)

            # run 1: no checkpoint -> everything lands
            r1 = await _run_and_wait(client, wf, {"items": [
                {"ts": "1", "v": "a"}, {"ts": "3", "v": "b"},
            ]})
            out1 = _find_node_run(r1, "w")["output"]
            assert out1["written"] == 2
            assert out1["checkpoint_after"] == "3"

            # run 2: baseline 0.5 -> ts 1 re-admitted (append: a,b,1c,9)
            r2 = await _run_and_wait(client, wf, {"items": [
                {"ts": "1", "v": "1c"}, {"ts": "9", "v": "new"},
            ]})
            out2 = _find_node_run(r2, "w")["output"]
            assert out2["written"] == 2
            assert out2["skipped"] == 0
            assert out2["checkpoint_before"] == "3"
            assert out2["checkpoint_after"] == "9"  # best seen, NOT the baseline
            assert out2["lookback"] == 2.5

            ds_id = next(d["id"] for d in (await client.get("/datasets")).json() if d["name"] == ds_name)
            states = {s["key"]: s for s in (await client.get(f"/datasets/{ds_id}/ingestion-states")).json()}
            assert states["pipe1"]["watermark"] == "9"
            assert states["pipe1"]["stats"]["mode"] == "incremental"
            assert states["pipe1"]["stats"]["written"] == 2
            assert states["pipe1"]["stats"]["lookback"] == 2.5

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ===========================================================================
# 4) engine: incremental UPSERT (watermark + key_columns) - the headline.
#    Late-arriving updates merge on key, never duplicate; lookback re-admits.
# ===========================================================================
def test_v53_incremental_upsert_engine():
    tag = uuid.uuid4().hex[:8]
    ds_name = f"v53-upsert-wm-{tag}"

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {
                        "dataset": ds_name,
                        "mode": "upsert",
                        "key_columns": ["id"],
                        "watermark_column": "ts",
                        "ingestion_key": "pipe1",
                    }),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            wf = await _make_workflow(client, f"v53-upsert-wm-{tag}", graph)

            # run 1: fresh checkpoint -> 2 inserts
            r1 = await _run_and_wait(client, wf, {"items": [
                {"id": "1", "city": "berlin", "ts": "10"},
                {"id": "2", "city": "paris", "ts": "20"},
            ]})
            out1 = _find_node_run(r1, "w")["output"]
            assert out1["written"] == 2 and out1["inserted"] == 2 and out1["updated"] == 0
            assert out1["checkpoint_after"] == "20"
            assert out1["watermark_column"] == "ts"  # upsert payload carries incremental fields

            # run 2: an UPDATE (ts beyond) + a NEW key -> merged, not duplicated
            r2 = await _run_and_wait(client, wf, {"items": [
                {"id": "2", "city": "paris-2", "ts": "30"},
                {"id": "3", "city": "rome", "ts": "25"},
            ]})
            out2 = _find_node_run(r2, "w")["output"]
            assert out2["written"] == 2 and out2["updated"] == 1 and out2["inserted"] == 1
            assert out2["checkpoint_after"] == "30"

            # run 3 with lookback=15 (baseline 15): id1's late correction re-admitted
            graph_lb = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {
                        "dataset": ds_name,
                        "mode": "upsert",
                        "key_columns": ["id"],
                        "watermark_column": "ts",
                        "ingestion_key": "pipe1",
                        "lookback": 15,
                    }),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            wf_lb = await _make_workflow(client, f"v53-upsert-wm-lb-{tag}", graph_lb)
            r3 = await _run_and_wait(client, wf_lb, {"items": [
                {"id": "1", "city": "berlin-2", "ts": "12"},  # 12 > baseline 15? no: 12 < 15... uses numeric 15-? baseline=30-15=15 -> 12 skipped
                {"id": "4", "city": "madrid", "ts": "40"},
            ]})
            out3 = _find_node_run(r3, "w")["output"]
            # ts 12 is below the rewound baseline (15) -> skipped; ts 40 lands
            assert out3["skipped"] == 1, out3
            assert out3["written"] == 1 and out3["inserted"] == 1 and out3["updated"] == 0
            assert out3["checkpoint_after"] == "40"

            # run 4: a late correction INSIDE the lookback window
            # (checkpoint 40, lookback 15 -> baseline 25; ts 26 re-admitted)
            r4 = await _run_and_wait(client, wf_lb, {"items": [
                {"id": "2", "city": "paris-final", "ts": "26"},
            ]})
            out4 = _find_node_run(r4, "w")["output"]
            assert out4["written"] == 1 and out4["updated"] == 1 and out4["inserted"] == 0, out4
            assert out4["checkpoint_after"] == "40"  # 26 is within the window, not a new high

            # the dataset: 4 rows, NO duplicates, latest values won
            ds_id = next(d["id"] for d in (await client.get("/datasets")).json() if d["name"] == ds_name)
            rows_res = await client.get(f"/datasets/{ds_id}/rows")
            rows = rows_res.json()["rows"]
            assert rows_res.json()["row_count"] == 4, rows
            by_id = {r["id"]: r["city"] for r in rows}
            assert by_id == {"1": "berlin", "2": "paris-final", "3": "rome", "4": "madrid"}, by_id

            # checkpoint stats on the state row (mode=upsert + counters)
            states = {s["key"]: s for s in (await client.get(f"/datasets/{ds_id}/ingestion-states")).json()}
            st = states["pipe1"]
            assert st["watermark"] == "40"
            assert st["stats"]["mode"] == "upsert"
            assert st["stats"]["updated"] == 1
            assert st["stats"]["lookback"] == 15

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ===========================================================================
# 5) empty payload is a clean no-op; lookback on mode=append is rejected
# ===========================================================================
def test_v53_incremental_guards():
    tag = uuid.uuid4().hex[:8]
    ds_name = f"v53-guards-{tag}"

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {
                        "dataset": ds_name,
                        "mode": "incremental",
                        "watermark_column": "ts",
                    }),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            wf = await _make_workflow(client, f"v53-guard-empty-{tag}", graph)

            # empty source -> success, nothing written, no checkpoint boom
            r1 = await _run_and_wait(client, wf, {"items": []})
            assert r1["status"] == "success", r1.get("error")
            out1 = _find_node_run(r1, "w")["output"]
            assert out1["written"] == 0 and out1["skipped"] == 0

            # non-empty rows missing the cursor column still fail loudly
            r2 = await _run_and_wait(client, wf, {"items": [{"nope": 1}]})
            assert r2["status"] == "error"

            # lookback with mode=append is a clean node error
            bad_graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {"dataset": ds_name, "lookback": 5}),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            bad_wf = await _make_workflow(client, f"v53-guard-lookback-{tag}", bad_graph)
            bad = await _run_and_wait(client, bad_wf, {"items": [{"x": 1}]})
            assert bad["status"] == "error"
            assert "lookback" in str(_find_node_run(bad, "w").get("error", "")).lower() or "lookback" in str(bad.get("error", "")).lower()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ===========================================================================
# 6) observability events: the unified, derived stream
# ===========================================================================
def test_v53_observability_events():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            # --- info: a dataset write (append -> a version row)
            ds_name = f"v53-obs-{tag}"
            ds_id = await _mk_dataset(client, ds_name, [{"a": 1}])
            res = await client.post(f"/datasets/{ds_id}/rows", json={"rows": [{"a": 2}]})
            assert res.status_code in (200, 201), res.text

            # --- info: a succeeded run
            ok_graph = {"nodes": [_node("t", "manual_trigger"), _node("s", "set_variable", {"assignments": {"x": "1"}})], "edges": [_edge("e1", "t", "s")]}
            ok_wf = await _make_workflow(client, f"v53-obs-ok-{tag}", ok_graph)
            ok_run = await _run_and_wait(client, ok_wf, {})
            assert ok_run["status"] == "success"

            # --- error: a failed run
            bad_graph = {"nodes": [_node("t", "manual_trigger"), _node("c", "code", {"code": "result = 1 / 0"})], "edges": [_edge("e1", "t", "c")]}
            bad_wf = await _make_workflow(client, f"v53-obs-bad-{tag}", bad_graph)
            bad_run = await _run_and_wait(client, bad_wf, {})
            assert bad_run["status"] == "error"

            # --- seeded rows for the sources that are awkward to reach via API
            from app.db import AsyncSessionLocal
            from app.models import App, Dashboard, DashboardAuditEvent, GrantAuditEvent, ReportDeliveryEvent, ScheduledReport

            async with AsyncSessionLocal() as session:
                rep = ScheduledReport(name=f"v53-obs-rep-{tag}", source_type="dataset", source_id=ds_id, fmt="csv", cron="0 6 * * *")
                session.add(rep)
                await session.flush()  # rep.id is server-defaulted - flush first
                session.add(ReportDeliveryEvent(report_id=rep.id, channel="email", target="ops@example.com", status="error", detail="SMTP not configured"))
                app_row = App(name=f"v53-obs-app-{tag}", slug=f"v53-obs-app-{tag}")
                dash_row = Dashboard(name=f"v53-obs-dash-{tag}", slug=f"v53-obs-dash-{tag}")
                session.add_all([app_row, dash_row])
                await session.flush()
                session.add(GrantAuditEvent(app_id=app_row.id, action="view_runtime", outcome="denied", detail="missing token"))
                session.add(DashboardAuditEvent(dashboard_id=dash_row.id, action="view_dashboard", outcome="denied", detail="invalid token"))
                await session.commit()

            # --- the stream has them all, newest first
            res = await client.get("/observability/events")
            assert res.status_code == 200, res.text
            body = res.json()
            types = {e["type"] for e in body["events"]}
            assert "dataset.written" in types, types
            assert "workflow.succeeded" in types, types
            assert "workflow.failed" in types, types
            assert "report.delivery_failed" in types, types
            assert "share.denied" in types, types

            evs = body["events"]
            ts_list = [e["ts"] for e in evs]
            assert ts_list == sorted(ts_list, reverse=True), "events must be newest-first"

            # envelope shape
            sample = next(e for e in evs if e["type"] == "workflow.failed")
            assert set(sample) >= {"id", "type", "ts", "severity", "title", "detail", "ref", "meta"}
            assert sample["severity"] == "error"
            assert "division by zero" in (sample["detail"] or "") or "zerodivision" in (sample["detail"] or "").lower()

            ds_events = [e for e in evs if e["type"] == "dataset.written" and e["meta"]["dataset"] == ds_name]
            assert ds_events, "dataset.written events expected"
            newest = ds_events[0]  # newest first: the append version
            assert newest["meta"]["row_count"] == 2 and newest["meta"]["write_source"] == "append"
            assert newest["ref"] == f"/datasets/{ds_id}"

            # --- type filter (prefix match)
            res = await client.get("/observability/events?type=workflow.")
            assert {e["type"] for e in res.json()["events"]} <= {"workflow.succeeded", "workflow.failed", "workflow.cancelled"}
            res = await client.get("/observability/events?type=report.delivery_failed")
            assert {e["type"] for e in res.json()["events"]} == {"report.delivery_failed"}

            # --- severity filter
            res = await client.get("/observability/events?severity=error")
            assert res.json()["events"], "error-severity events expected"
            assert {e["severity"] for e in res.json()["events"]} == {"error"}

            # --- limit/offset pagination
            res = await client.get("/observability/events?limit=1")
            assert len(res.json()["events"]) == 1 and res.json()["total"] >= 1
            res = await client.get("/observability/events?limit=1&offset=1")
            assert len(res.json()["events"]) == 1

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ===========================================================================
# 7) observability overview: fleet-wide, budget-capped, derived
# ===========================================================================
def test_v53_observability_overview():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            # a dataset + one incremental checkpoint via the engine
            ds_name = f"v53-ov-ds-{tag}"
            ds_id = await _mk_dataset(client, ds_name, [{"a": 1, "b": "x"}])
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {"dataset": f"{ds_name}-incr", "mode": "incremental", "watermark_column": "ts"}),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            wf = await _make_workflow(client, f"v53-ov-wf-{tag}", graph)
            run = await _run_and_wait(client, wf, {"items": [{"ts": "1", "a": 2}]})
            assert run["status"] == "success", run.get("error")

            res = await client.get("/observability/overview")
            assert res.status_code == 200, res.text
            ov = res.json()

            # shape
            assert set(ov) >= {"overall", "generated_at", "datasets", "pipelines", "ingestion", "deliveries", "incidents"}
            d = ov["datasets"]
            assert set(d) >= {"total", "healthy", "degraded", "unhealthy", "violating_contracts", "stale_or_cold", "rows_total", "scored", "unscored"}
            assert d["total"] >= 2, d  # the created dataset + the incremental one
            assert d["healthy"] + d["degraded"] + d["unhealthy"] == d["scored"]

            p = ov["pipelines"]
            assert p["workflows_total"] >= 1
            assert p["runs_24h"] >= 1
            assert p["runs_7d"] >= 1
            assert isinstance(p["failure_rate_7d"], (int, float))
            assert isinstance(p["failing_workflows"], list)

            ing = ov["ingestion"]
            assert ing["checkpoints"] >= 1, ing
            assert any(pipe["key"] == "default" and pipe["watermark"] == "1" for pipe in ing["pipelines"]), ing
            assert ing["rows_total"] >= 1

            assert set(ov["deliveries"]) >= {"ok_7d", "error_7d", "skipped_7d"}
            assert isinstance(ov["incidents"], list)

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ===========================================================================
# 8) node definition: the new lookback field auto-generates into the UI form
# ===========================================================================
def test_v53_definitions():
    async def _go():
        async with _client() as client:
            res = await client.get("/node-definitions")
            assert res.status_code == 200, res.text
            defs = {d["type"]: d for d in res.json()["definitions"]}
            dw = defs["dataset_write"]
            blob = __import__("json").dumps(dw)
            assert "lookback" in blob
            # watermark description now mentions the upsert combo
            assert "upsert mode" in blob or "incremental upsert" in blob.lower() or "watermark_column" in blob

    asyncio.run(_go())
