"""V45 feature tests: the deep data-estate wave I - engineering & analysis.

New nodes: join (pandas-backed inner/left/right/outer/anti), pivot, unpivot,
cast_columns, handle_nulls, data_quality, analyze (descriptive / correlation /
outliers / distribution / value_counts / trend), dataset_export; dataset_write
gains upsert mode; Summarize gains count_distinct / median / std / first / last /
concat + having / sort / limit; datasets gain profile v2 (quantiles, outliers,
correlations, duplicates) and a download endpoint (csv/xlsx/json/parquet).

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v44).
"""

from __future__ import annotations

import asyncio
import json
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


async def _cleanup(client_unused: None, workflow_ids: list[str], dataset_refs: list[str], artifact_ids: list[str]) -> None:
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
        for ref in dataset_refs:
            try:
                await client.delete(f"/datasets/{ref}")
            except Exception:
                pass
        for aid in artifact_ids:
            try:
                await client.delete(f"/artifacts/{aid}")
            except Exception:
                pass
    await _drain_background()


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": name or nid, "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str, source_handle: str = "main", target_handle: str = "main") -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": source_handle, "targetHandle": target_handle}


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


def _sales_rows() -> list[dict]:
    return [
        {"id": i, "region": ["east", "west", "north"][i % 3], "amount": 10 + i, "qty": i % 4}
        for i in range(1, 13)
    ]


# ---------------------------------------------------------------------------
# 1) Definitions: the v45 nodes exposed
# ---------------------------------------------------------------------------
def test_v45_definitions():
    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.status_code == 200
            assert res.json()["version"] == "1.45.0", f"expected strict pin 1.45.0, got {res.json()['version']}"
            res = await client.get("/node-definitions")
            assert res.status_code == 200
            defs = res.json()["definitions"]
            types = [d["type"] for d in defs]
            assert len(types) == 45, f"expected 45 visible types at v45, got {len(types)}"
            by = {d["type"]: d for d in defs}
            for t in ("join", "pivot", "unpivot", "cast_columns", "handle_nulls", "data_quality", "analyze", "dataset_export"):
                assert t in types, t
            props = {t: set(by[t]["parameters_schema"]["properties"].keys()) for t in by if t in ("join", "pivot", "analyze", "dataset_export")}
            assert props["join"] == {"left_field", "right_field", "how", "suffix_right"}
            assert props["pivot"] == {"index", "pivot_on", "value", "agg"}
            assert props["analyze"] == {
                "analysis", "columns", "method", "threshold", "bins",
                "max_values", "timestamp", "freq", "metric", "value_column",
            }
            assert props["dataset_export"] == {"dataset", "fmt"}
            assert by["join"]["inputs"] == [
                {"key": "main", "label": "Input A"},
                {"key": "secondary", "label": "Input B"},
            ]

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], []))


# ---------------------------------------------------------------------------
# 2) Summarize v2: count_distinct / median / having / sort / limit
# ---------------------------------------------------------------------------
def test_v45_summarize_deep_ops():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("s", "summarize", {
                        "group_by": ["region"],
                        "aggregates": [
                            {"field": "amount", "op": "sum"},
                            {"field": "id", "op": "count_distinct"},
                            {"field": "amount", "op": "median"},
                        ],
                        "having": [{"label": "id_count_distinct", "op": ">=", "value": 4}],
                        "sort_by": "amount_sum",
                        "sort_dir": "desc",
                        "limit": 2,
                    }),
                ],
                "edges": [_edge("e1", "t", "s")],
            }
            wf = await _make_workflow(client, f"v45-summarize-{tag}", graph)
            exec_row = await _run_and_wait(client, wf, {"items": _sales_rows()})
            assert exec_row["status"] == "success", exec_row.get("error")
            run = _find_node_run(exec_row, "s")
            assert run["status"] == "success", run
            out = run["output"]
            # having keeps only groups with >= 4 distinct ids (all 3 qualify);
            # sort desc by amount_sum; limit 2 keeps the top two
            assert out["groups"] == 2
            sums = [row["amount_sum"] for row in out["items"]]
            assert sums == sorted(sums, reverse=True)
            top = out["items"][0]
            # region = ['east','west','north'][i % 3] → east: ids 3,6,9,12 → 13+16+19+22 = 70
            assert top["region"] == "east"
            assert top["amount_sum"] == 70
            assert top["id_count_distinct"] == 4
            assert top["amount_median"] == 17.5

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], []))


# ---------------------------------------------------------------------------
# 3) Join: inner / left / anti over two datasets
# ---------------------------------------------------------------------------
def test_v45_join_nodes():
    tag = uuid.uuid4().hex[:8]
    orders = [
        {"id": 1, "customer": "alice", "total": 100},
        {"id": 2, "customer": "bob", "total": 200},
        {"id": 3, "customer": "carol", "total": 300},
    ]
    plans = [
        {"cid": 1, "plan": "pro"},
        {"cid": 3, "plan": "free"},
        {"cid": 3, "plan": "dup"},  # duplicate key exercises m:N fidelity
    ]

    async def _go():
        async with _client() as client:
            for how, expect_rows, expect_matched in (
                ("inner", 3, 3),   # alice + carol×2 (m:N)
                ("left", 4, 3),    # bob kept with null plan
                ("anti", 1, 0),    # only bob has no plan
            ):
                graph = {
                    "nodes": [
                        _node("t", "manual_trigger"),
                        _node("sa", "split_out", {"field": "a"}, "Orders"),
                        _node("sb", "split_out", {"field": "b"}, "Plans"),
                        _node("j", "join", {"left_field": "id", "right_field": "cid", "how": how}),
                    ],
                    "edges": [
                        _edge("e1", "t", "sa"),
                        _edge("e2", "t", "sb"),
                        _edge("e3", "sa", "j", "main", "main"),
                        _edge("e4", "sb", "j", "main", "secondary"),
                    ],
                }
                wf = await _make_workflow(client, f"v45-join-{how}-{tag}", graph)
                exec_row = await _run_and_wait(client, wf, {"a": orders, "b": plans})
                assert exec_row["status"] == "success", exec_row.get("error")
                run = _find_node_run(exec_row, "j")
                assert run["status"] == "success", run
                out = run["output"]
                assert out["rows_out"] == expect_rows, (how, out)
                assert out["matched"] == expect_matched, (how, out)
                if how == "anti":
                    assert out["items"][0]["customer"] == "bob"
                elif how == "left":
                    assert out["left_only"] == 1  # bob unmatched
                    bob = next(r for r in out["items"] if r["customer"] == "bob")
                    assert bob["plan"] is None  # left join keeps unmatched with nulls
                else:
                    names = {r["customer"] for r in out["items"]}
                    assert "alice" in names and "carol" in names

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], []))


# ---------------------------------------------------------------------------
# 4) Pivot → Unpivot round trip
# ---------------------------------------------------------------------------
def test_v45_pivot_unpivot():
    tag = uuid.uuid4().hex[:8]

    def _pq_rows():
        rows = []
        for r_i, region in enumerate(["east", "west"]):
            for q_i, quarter in enumerate(["q1", "q2"]):
                rows.append({"region": region, "quarter": quarter, "amount": (r_i + 1) * 10 + q_i})
        return rows

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("p", "pivot", {"index": ["region"], "pivot_on": "quarter", "value": "amount", "agg": "sum"}),
                ],
                "edges": [_edge("e1", "t", "p")],
            }
            wf = await _make_workflow(client, f"v45-pivot-{tag}", graph)
            exec_row = await _run_and_wait(client, wf, {"items": _pq_rows()})
            run = _find_node_run(exec_row, "p")
            assert run["status"] == "success", run
            pivoted = run["output"]["items"]
            assert len(pivoted) == 2  # one row per region
            east = next(r for r in pivoted if r["region"] == "east")
            assert east["q1"] == 10 and east["q2"] == 11

            # unpivot brings it back to tidy (region, variable, value) rows
            graph2 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("u", "unpivot", {"index": ["region"], "columns": ["q1", "q2"], "var_name": "k", "value_name": "v"}),
                ],
                "edges": [_edge("e1", "t", "u")],
            }
            wf2 = await _make_workflow(client, f"v45-unpivot-{tag}", graph2)
            exec_row2 = await _run_and_wait(client, wf2, {"items": pivoted})
            run2 = _find_node_run(exec_row2, "u")
            assert run2["status"] == "success", run2
            tidy = run2["output"]["items"]
            assert len(tidy) == 4  # 2 regions x 2 melted columns
            assert {"k", "v", "region"} <= set(tidy[0].keys())
            east_q2 = next(r for r in tidy if r["region"] == "east" and r["k"] == "q2")
            assert east_q2["v"] == 11

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], []))


# ---------------------------------------------------------------------------
# 5) cast_columns + handle_nulls
# ---------------------------------------------------------------------------
def test_v45_cast_and_nulls():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            rows = [
                {"when": "2024-01-15", "age": "31", "score": "9.5", "ok": "true", "note": "x"},
                {"when": "2024-02-01", "age": "44", "score": "7.0", "ok": "false", "note": "y"},
                {"when": "not-a-date", "age": "oops", "score": None, "ok": None, "note": None},
            ]
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("c", "cast_columns", {
                        "casts": [
                            {"column": "when", "dtype": "datetime", "format": "%Y-%m-%d"},
                            {"column": "age", "dtype": "integer"},
                            {"column": "score", "dtype": "number"},
                            {"column": "ok", "dtype": "boolean"},
                        ],
                        "on_error": "coerce",
                    }),
                    _node("n", "handle_nulls", {"mode": "drop", "columns": ["when", "age"]}),
                ],
                "edges": [_edge("e1", "t", "c"), _edge("e2", "c", "n")],
            }
            wf = await _make_workflow(client, f"v45-cast-{tag}", graph)
            exec_row = await _run_and_wait(client, wf, {"items": rows})
            assert exec_row["status"] == "success", exec_row.get("error")
            cast_run = _find_node_run(exec_row, "c")
            assert cast_run["status"] == "success", cast_run
            out = cast_run["output"]
            assert out["cast"] == ["when", "age", "score", "ok"]
            bad = out["items"][2]
            assert bad["when"] is None  # coerced, not crashed
            assert bad["age"] is None
            assert bad["ok"] is None

            drop_run = _find_node_run(exec_row, "n")
            assert drop_run["status"] == "success", drop_run
            assert drop_run["output"]["rows_out"] == 2  # the coerced-null row was dropped
            assert drop_run["output"]["dropped"] == 1

            # fill mode: mean fill on score
            graph2 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("f", "handle_nulls", {"mode": "fill", "columns": ["score"], "fill": "mean"}),
                ],
                "edges": [_edge("e1", "t", "f")],
            }
            wf2 = await _make_workflow(client, f"v45-fill-{tag}", graph2)
            exec_row2 = await _run_and_wait(client, wf2, {"items": rows})
            fill_run = _find_node_run(exec_row2, "f")
            assert fill_run["status"] == "success", fill_run
            assert fill_run["output"]["filled"] == 1
            filled_rows = fill_run["output"]["items"]
            assert abs(filled_rows[2]["score"] - 8.25) < 0.01  # mean(9.5, 7.0)

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], []))


# ---------------------------------------------------------------------------
# 6) data_quality: warn report + error gate + schema check
# ---------------------------------------------------------------------------
def test_v45_data_quality():
    tag = uuid.uuid4().hex[:8]
    rows = [
        {"email": "a@x.com", "age": 30, "plan": "pro"},
        {"email": "b@x.com", "age": 200, "plan": "enterprise"},
        {"email": None, "age": -5, "plan": "pro"},
    ]

    async def _go():
        async with _client() as client:
            checks = [
                {"check": "not_null", "column": "email"},
                {"check": "range", "column": "age", "min": 0, "max": 120},
                {"check": "allowed_values", "column": "plan", "values": ["pro", "free"]},
                {"check": "min_rows", "min": 2},
                {"check": "schema", "expected": [{"name": "email", "dtype": "text"}]},
            ]
            # warn mode: run succeeds, report flags 2 failing checks
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("q", "data_quality", {"checks": checks, "on_fail": "warn"}),
                ],
                "edges": [_edge("e1", "t", "q")],
            }
            wf = await _make_workflow(client, f"v45-dq-warn-{tag}", graph)
            exec_row = await _run_and_wait(client, wf, {"items": rows})
            assert exec_row["status"] == "success", exec_row.get("error")
            run = _find_node_run(exec_row, "q")
            out = run["output"]
            assert out["passed"] is False
            assert out["total_checks"] == 5
            assert out["failed_checks"] == 3  # not_null(1) + range(2: 200 & -5) + allowed_values(1)
            by_check = {c["check"]: c for c in out["checks"]}
            assert by_check["not_null"]["failed"] == 1
            assert by_check["range"]["failed"] == 2
            assert by_check["allowed_values"]["failed"] == 1  # 'enterprise' not allowed
            assert by_check["min_rows"]["passed"] is True
            assert by_check["schema"]["passed"] is True

            # error mode: the run FAILS with a data-quality message
            graph2 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("q", "data_quality", {"checks": checks[:1], "on_fail": "error"}),
                ],
                "edges": [_edge("e1", "t", "q")],
            }
            wf2 = await _make_workflow(client, f"v45-dq-error-{tag}", graph2)
            exec_row2 = await _run_and_wait(client, wf2, {"items": rows})
            assert exec_row2["status"] == "error"
            assert "Data quality failed" in (exec_row2.get("error") or "")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], []))


# ---------------------------------------------------------------------------
# 7) analyze: descriptive / outliers / trend / correlation / value_counts
# ---------------------------------------------------------------------------
def test_v45_analyze_modes():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            # day 9 is a deliberate revenue spike (an IQR outlier)
            rows = [{"day": f"2024-03-{d:02d}", "revenue": float(100 + d * 7 + (500 if d == 9 else 0)), "channel": "web" if d % 2 else "retail"} for d in range(1, 11)]
            cases = [
                ("descriptive", {"columns": ["revenue"]}, lambda out: _assert_descriptive(out)),
                ("outliers", {"columns": ["revenue"], "method": "iqr", "threshold": 1.5}, lambda out: _assert_outliers(out)),
                ("trend", {"timestamp": "day", "freq": "day", "metric": "sum", "value_column": "revenue"}, lambda out: _assert_trend(out)),
                ("correlation", {"columns": ["revenue"]}, None),  # single col → error expected
                ("value_counts", {"columns": ["channel"]}, lambda out: _assert_value_counts(out)),
            ]
            for i, (analysis, params, check) in enumerate(cases):
                graph = {
                    "nodes": [
                        _node("t", "manual_trigger"),
                        _node("a", "analyze", {"analysis": analysis, **params}),
                    ],
                    "edges": [_edge("e1", "t", "a")],
                }
                wf = await _make_workflow(client, f"v45-analyze-{analysis}-{tag}", graph)
                exec_row = await _run_and_wait(client, wf, {"items": rows})
                run = _find_node_run(exec_row, "a")
                if analysis == "correlation":
                    # only one numeric column passed → node errors with a clear message
                    assert exec_row["status"] == "error"
                    assert "at least 2 numeric columns" in (exec_row.get("error") or "")
                    continue
                assert exec_row["status"] == "success", (analysis, exec_row.get("error"))
                assert run["status"] == "success", run
                if check:
                    check(run["output"])

    def _assert_descriptive(out):
        col = out["columns"][0]
        assert col["column"] == "revenue"
        assert col["count"] == 10
        assert col["min"] < col["q25"] < col["median"] < col["q75"] < col["max"]
        assert col["std"] > 0

    def _assert_outliers(out):
        found = out["columns"][0]
        assert found["outlier_count"] >= 1  # the day-9 spike
        assert found["outlier_rows"][0]["revenue"] > found["upper"]

    def _assert_trend(out):
        assert len(out["points"]) == 10
        assert out["growth"]["first"] is not None and out["growth"]["last"] is not None
        assert out["growth"]["last"] > out["growth"]["first"]  # spike inflates the last bucket? no - last is day 10; assert absolute levels instead
        assert out["growth"]["pct_change"] != 0

    def _assert_value_counts(out):
        values = {v["value"]: v["count"] for v in out["values"]}
        assert values == {"web": 5, "retail": 5}
        assert abs(sum(v["pct"] for v in out["values"]) - 100) < 0.1

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], []))


# ---------------------------------------------------------------------------
# 8) dataset_write upsert mode
# ---------------------------------------------------------------------------
def test_v45_dataset_upsert():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v45-upsert-{tag}", [
                {"email": "a@x.com", "ltv": 100},
                {"email": "b@x.com", "ltv": 200},
            ])
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {
                        "dataset": f"v45-upsert-{tag}",
                        "mode": "upsert",
                        "key_columns": ["email"],
                    }),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            wf = await _make_workflow(client, f"v45-upsert-run-{tag}", graph)
            # 1 existing key (a) updated + 1 new key (c) inserted
            exec_row = await _run_and_wait(client, wf, {"items": [
                {"email": "a@x.com", "ltv": 999},
                {"email": "c@x.com", "ltv": 50},
            ]})
            assert exec_row["status"] == "success", exec_row.get("error")
            run = _find_node_run(exec_row, "w")
            out = run["output"]
            assert out["written"] == 2
            assert out["updated"] == 1
            assert out["inserted"] == 1
            assert out["row_count"] == 3  # 2 originals + 1 new (a replaced in place)

            res = await client.get(f"/datasets/{ds_id}/rows?limit=100")
            rows = {r["email"]: r["ltv"] for r in res.json()["rows"]}
            assert rows == {"a@x.com": 999, "b@x.com": 200, "c@x.com": 50}

            # upsert without key_columns is a clean node error
            graph2 = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("w", "dataset_write", {"dataset": f"v45-upsert-{tag}", "mode": "upsert"}),
                ],
                "edges": [_edge("e1", "t", "w")],
            }
            wf2 = await _make_workflow(client, f"v45-upsert-nokey-{tag}", graph2)
            exec_row2 = await _run_and_wait(client, wf2, {"items": [{"email": "z@x.com"}]})
            assert exec_row2["status"] == "error"
            assert "key_columns" in (exec_row2.get("error") or "")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], []))


# ---------------------------------------------------------------------------
# 9) dataset_export node → artifact + the download endpoint
# ---------------------------------------------------------------------------
def test_v45_export_node_and_endpoint():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            ds_id = await _mk_dataset(client, f"v45-export-{tag}", [
                {"name": "alice", "ltv": 100},
                {"name": "bob", "ltv": 200},
            ])
            graph = {
                "nodes": [
                    _node("t", "manual_trigger"),
                    _node("e", "dataset_export", {"dataset": f"v45-export-{tag}", "fmt": "csv"}),
                ],
                "edges": [_edge("e1", "t", "e")],
            }
            wf = await _make_workflow(client, f"v45-export-run-{tag}", graph)
            exec_row = await _run_and_wait(client, wf, {})
            assert exec_row["status"] == "success", exec_row.get("error")
            run = _find_node_run(exec_row, "e")
            out = run["output"]
            assert out["rows"] == 2
            assert out["artifact_id"]
            assert out["artifact_url"].endswith("/content")

            # artifact content is real CSV
            res = await client.get(f"/artifacts/{out['artifact_id']}/content")
            assert res.status_code == 200
            assert b"alice" in res.content and b"100" in res.content

            # direct download endpoint (xlsx path exercises openpyxl too)
            for fmt in ("csv", "json", "xlsx"):
                dl = await client.get(f"/datasets/{ds_id}/export?fmt={fmt}")
                assert dl.status_code == 200, (fmt, dl.text)
                assert "attachment" in dl.headers["content-disposition"]
                if fmt == "json":
                    parsed = json.loads(dl.content)
                    assert {r["name"] for r in parsed} == {"alice", "bob"}
                if fmt == "csv":
                    assert dl.content.startswith(b"\xef\xbb\xbfname,ltv")  # utf-8-sig BOM for Excel
                    assert b"alice,100" in dl.content

            # bad format → 400
            bad = await client.get(f"/datasets/{ds_id}/export?fmt=tsv")
            assert bad.status_code == 400

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], []))


# ---------------------------------------------------------------------------
# 10) profile v2: backward-compatible + deep stats
# ---------------------------------------------------------------------------
def test_v45_profile_v2():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            rows = []
            for i in range(1, 13):
                rows.append({
                    "id": i,
                    "score": i * 10 if i != 12 else 1000,  # 1000 is an IQR outlier
                    "segment": ["a", "b"][i % 2],
                    "when": f"2024-01-{i:02d}T10:00:00",
                })
            ds_id = await _mk_dataset(client, f"v45-profile-{tag}", rows)
            res = await client.get(f"/datasets/{ds_id}/profile")
            assert res.status_code == 200
            profile = res.json()

            # dataset level
            assert profile["row_count"] == 12
            assert profile["column_count"] == 4
            assert profile["duplicate_rows"] == 0
            assert profile["completeness_pct"] == 100.0
            # every value in id/score/when is distinct → all three flagged id-like
            assert set(profile["id_like_columns"]) == {"id", "score", "when"}
            assert len(profile["correlation"]) == 2  # id + score are numeric
            corr_by_col = {c["column"]: c["correlations"] for c in profile["correlation"]}
            assert abs(corr_by_col["id"]["score"]) > 0.3  # positive, dented by the spike
            assert corr_by_col["id"]["id"] == 1.0

            # column level - backward keys still present
            by_col = {c["name"]: c for c in profile["columns"]}
            assert by_col["id"]["non_null"] == 12 and by_col["id"]["nulls"] == 0
            score = by_col["score"]
            assert score["min"] == 10 and score["max"] == 1000
            assert "std" in score and "q25" in score and "median" in score and "q75" in score
            assert score["outliers_iqr"] == 1
            assert score["outlier_sample"][0]["score"] == 1000
            seg = by_col["segment"]
            assert seg["top_values"] and seg["top_values"][0]["count"] == 6
            # ISO strings land as text (JSON ingestion), but the profile
            # detects the datetime shape and adds time-coverage stats
            when = by_col["when"]
            assert when["dtype"] == "text"
            assert when["parsed_as_datetime"] is True
            assert when["span_days"] > 0
            assert when["datetime_min"].startswith("2024-01-01")
            assert by_col["id"]["null_pct"] == 0.0

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(None, [], [], []))
